"""RDS/Aurora provisioning via boto3 (FR-38/46/52, DESIGN §8).

Execution uses the AWS SDK (not the CLI). Guide/generate-only mode emits the AWS CLI
equivalents instead (see phases/guide.py). Physical-seed strategies are same-engine
only (RDS→RDS or Aurora→Aurora), preserving LSN continuity (FR-49).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import boto3

from pgreplkit.logconf import get_logger

log = get_logger()


@dataclass
class RdsClient:
    profile: str | None = None
    region: str = "us-east-1"

    def client(self):
        session = boto3.Session(profile_name=self.profile, region_name=self.region)
        return session.client("rds")


def describe_instance(rds, instance_id: str) -> dict | None:
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=instance_id)
        return resp["DBInstances"][0]
    except rds.exceptions.DBInstanceNotFoundFault:
        return None


def endpoint_of(instance: dict) -> tuple[str, int]:
    ep = instance["Endpoint"]
    return ep["Address"], int(ep["Port"])


def wait_available(rds, instance_id: str, *, timeout_min: int = 20) -> dict:
    log.info("waiting for RDS instance %s to become available...", instance_id)
    waiter = rds.get_waiter("db_instance_available")
    waiter.wait(
        DBInstanceIdentifier=instance_id,
        WaiterConfig={"Delay": 20, "MaxAttempts": max(1, timeout_min * 3)},
    )
    return describe_instance(rds, instance_id)


def create_snapshot(rds, instance_id: str, snapshot_id: str) -> None:
    log.info("creating snapshot %s of %s", snapshot_id, instance_id)
    rds.create_db_snapshot(
        DBSnapshotIdentifier=snapshot_id, DBInstanceIdentifier=instance_id
    )
    rds.get_waiter("db_snapshot_available").wait(
        DBSnapshotIdentifier=snapshot_id,
        WaiterConfig={"Delay": 20, "MaxAttempts": 90},
    )


def restore_instance_from_snapshot(
    rds,
    snapshot_id: str,
    target_id: str,
    *,
    instance_class: str,
    subnet_group: str | None = None,
    security_group_ids: list[str] | None = None,
    parameter_group: str | None = None,
    publicly_accessible: bool = True,
) -> dict:
    """Restore a same-engine RDS instance from a snapshot (FR-46 snapshot-restore)."""
    log.info("restoring %s from snapshot %s (%s)", target_id, snapshot_id, instance_class)
    kwargs: dict = {
        "DBInstanceIdentifier": target_id,
        "DBSnapshotIdentifier": snapshot_id,
        "DBInstanceClass": instance_class,
        "PubliclyAccessible": publicly_accessible,
    }
    if subnet_group:
        kwargs["DBSubnetGroupName"] = subnet_group
    if parameter_group:
        kwargs["DBParameterGroupName"] = parameter_group
    rds.restore_db_instance_from_db_snapshot(**kwargs)
    inst = wait_available(rds, target_id)
    if security_group_ids:
        rds.modify_db_instance(
            DBInstanceIdentifier=target_id,
            VpcSecurityGroupIds=security_group_ids,
            ApplyImmediately=True,
        )
        inst = wait_available(rds, target_id)
    return inst


def delete_instance(rds, instance_id: str) -> None:
    log.info("deleting RDS instance %s", instance_id)
    rds.delete_db_instance(
        DBInstanceIdentifier=instance_id,
        SkipFinalSnapshot=True,
        DeleteAutomatedBackups=True,
    )


def delete_snapshot(rds, snapshot_id: str) -> None:
    try:
        rds.delete_db_snapshot(DBSnapshotIdentifier=snapshot_id)
    except rds.exceptions.DBSnapshotNotFoundFault:
        pass


# --- Aurora fast clone (copy-on-write) -------------------------------------------------

def cluster_endpoint(rds, cluster_id: str) -> tuple[str, int]:
    c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    return c["Endpoint"], int(c["Port"])


def wait_cluster_available(rds, cluster_id: str, *, timeout_min: int = 20) -> None:
    log.info("waiting for Aurora cluster %s ...", cluster_id)
    for _ in range(timeout_min * 3):
        c = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
        if c["Status"] == "available":
            return
        time.sleep(20)
    raise TimeoutError(f"cluster {cluster_id} not available in {timeout_min}m")


def clone_aurora_cluster(
    rds,
    source_cluster_id: str,
    target_cluster_id: str,
    *,
    instance_class: str,
    engine: str = "aurora-postgresql",
    subnet_group: str | None = None,
    security_group_ids: list[str] | None = None,
    cluster_parameter_group: str | None = None,
    publicly_accessible: bool = True,
) -> tuple[str, int]:
    """Aurora fast clone (copy-on-write) of a cluster + a writer instance (FR-46).

    Returns the clone cluster's (endpoint, port). The clone shares the source volume
    (copy-on-write) and preserves LSN continuity, so aurora_volume_logical_start_lsn()
    on the clone yields the consistent seed LSN.
    """
    log.info("fast-cloning cluster %s -> %s (copy-on-write)", source_cluster_id, target_cluster_id)
    kwargs: dict = {
        "DBClusterIdentifier": target_cluster_id,
        "SourceDBClusterIdentifier": source_cluster_id,
        "RestoreType": "copy-on-write",
        "UseLatestRestorableTime": True,
    }
    if subnet_group:
        kwargs["DBSubnetGroupName"] = subnet_group
    if security_group_ids:
        kwargs["VpcSecurityGroupIds"] = security_group_ids
    if cluster_parameter_group:
        kwargs["DBClusterParameterGroupName"] = cluster_parameter_group
    rds.restore_db_cluster_to_point_in_time(**kwargs)
    wait_cluster_available(rds, target_cluster_id)

    rds.create_db_instance(
        DBClusterIdentifier=target_cluster_id,
        DBInstanceIdentifier=f"{target_cluster_id}-1",
        DBInstanceClass=instance_class,
        Engine=engine,
        PubliclyAccessible=publicly_accessible,
    )
    wait_available(rds, f"{target_cluster_id}-1")
    return cluster_endpoint(rds, target_cluster_id)


def delete_aurora_cluster(rds, cluster_id: str) -> None:
    """Delete an Aurora cluster and its instances."""
    try:
        members = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)[
            "DBClusters"
        ][0]["DBClusterMembers"]
    except rds.exceptions.DBClusterNotFoundFault:
        return
    for m in members:
        try:
            rds.delete_db_instance(
                DBInstanceIdentifier=m["DBInstanceIdentifier"], SkipFinalSnapshot=True
            )
        except rds.exceptions.DBInstanceNotFoundFault:
            pass
    rds.delete_db_cluster(DBClusterIdentifier=cluster_id, SkipFinalSnapshot=True)
