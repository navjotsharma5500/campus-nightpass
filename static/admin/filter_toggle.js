(function () {
  function initFilterToggle() {
    if (!document.body.classList.contains("change-list")) return;

    var filter = document.getElementById("changelist-filter");
    if (!filter) return;

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "np-filter-toggle";
    btn.textContent = "Show Filter";
    btn.setAttribute("aria-expanded", "false");

    btn.addEventListener("click", function () {
      var isOpen = document.body.classList.toggle("np-filter-open");
      btn.textContent = isOpen ? "Hide Filter" : "Show Filter";
      btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });

    document.body.appendChild(btn);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initFilterToggle);
  } else {
    initFilterToggle();
  }
})();
