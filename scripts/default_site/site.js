/* Uruk default site — progressive enhancement only. The site is fully usable without this file. */
(function () {
  "use strict";
  document.documentElement.classList.add("has-js");

  // Mark the current page in the shared header (one header element serves every page).
  var path = location.pathname.replace(/\/+$/, "") || "/";
  document.querySelectorAll(".site-nav a").forEach(function (a) {
    var href = a.getAttribute("href").replace(/\/+$/, "") || "/";
    if (href === path) a.setAttribute("aria-current", "page");
  });

  // Reveal sections as they enter the viewport (skipped when reduced motion is set).
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var targets = document.querySelectorAll(".reveal");
  if (reduced || !("IntersectionObserver" in window)) {
    targets.forEach(function (el) { el.classList.add("is-in"); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    targets.forEach(function (el) { io.observe(el); });
  }

  // Copy-to-clipboard on terminal blocks.
  document.querySelectorAll("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var pre = btn.closest(".term").querySelector("pre");
      var text = Array.prototype.map
        .call(pre.querySelectorAll("[data-cmd]"), function (n) { return n.textContent; })
        .join("\n") || pre.textContent.trim();
      navigator.clipboard.writeText(text).then(function () {
        var label = btn.textContent;
        btn.textContent = "copied";
        setTimeout(function () { btn.textContent = label; }, 1600);
      });
    });
  });
})();
