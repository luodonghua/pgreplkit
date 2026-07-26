/* ==========================================================================
   clipboard.js — Attaches a "Copy" button to every .code-block on the page.

   Usage: call ClipboardUtil.enhanceAll(rootElement) after content is injected.
   Code blocks are expected to be:
     <div class="code-block">
       <span class="code-label">bash</span>       (optional)
       <pre><code>...</code></pre>
     </div>
   ========================================================================== */

(function (global) {
  "use strict";

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // Fallback for older / non-secure contexts.
    return new Promise((resolve, reject) => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        resolve();
      } catch (e) {
        reject(e);
      } finally {
        document.body.removeChild(ta);
      }
    });
  }

  function enhance(block) {
    if (block.querySelector(".copy-btn")) return; // already enhanced
    const pre = block.querySelector("pre");
    if (!pre) return;

    const btn = document.createElement("button");
    btn.className = "copy-btn";
    btn.type = "button";
    btn.textContent = "Copy";
    btn.setAttribute("aria-label", "Copy code to clipboard");

    btn.addEventListener("click", () => {
      const code = pre.innerText;
      copyText(code).then(
        () => {
          btn.textContent = "Copied!";
          btn.classList.add("copied");
          setTimeout(() => {
            btn.textContent = "Copy";
            btn.classList.remove("copied");
          }, 1600);
        },
        () => {
          btn.textContent = "Press Ctrl+C";
        }
      );
    });

    block.appendChild(btn);
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll(".code-block").forEach(enhance);
  }

  global.ClipboardUtil = { enhanceAll };
})(window);
