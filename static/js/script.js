(function () {
    var toggle = document.getElementById("menuToggle");
    var sidebar = document.getElementById("appSidebar");
    var backdrop = document.getElementById("sidebarBackdrop");

    if (!toggle || !sidebar || !backdrop) {
        return;
    }

    function closeMenu() {
        sidebar.classList.remove("open");
        backdrop.classList.remove("visible");
    }

    toggle.addEventListener("click", function () {
        sidebar.classList.toggle("open");
        backdrop.classList.toggle("visible");
    });

    backdrop.addEventListener("click", closeMenu);
})();

document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-reveal-spoiler]");
    if (!button) {
        return;
    }
    var target = document.getElementById(button.getAttribute("data-reveal-spoiler"));
    if (target) {
        target.classList.remove("spoiler-hidden");
        button.remove();
    }
});
