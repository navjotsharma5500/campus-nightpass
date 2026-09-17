(function () {
    'use strict';

    const MEASUREMENT_ID = 'G-4H69S8KET4';
    const PRODUCTION_HOSTNAME = 'campusconnect.thapar.edu';

    function isTrackedPath(pathname) {
        return pathname === '/permissions/' ||
            pathname === '/permissions/access/' ||
            pathname === '/permissions/admin/login/';
    }

    // Browser pathname excludes queries/fragments. Keep exact trailing-slash matches.
    const pagePath = window.location.pathname;
    if (window.location.hostname !== PRODUCTION_HOSTNAME || !isTrackedPath(pagePath)) {
        return;
    }

    const pageLocation = window.location.href;
    const sentLocations = window.__nightpassGa4Pageviews || new Set();
    window.__nightpassGa4Pageviews = sentLocations;
    if (sentLocations.has(pageLocation)) {
        return;
    }
    sentLocations.add(pageLocation);

    window.dataLayer = window.dataLayer || [];
    window.gtag = window.gtag || function () {
        window.dataLayer.push(arguments);
    };

    if (!window.__nightpassGa4Initialized) {
        window.__nightpassGa4Initialized = true;
        window.gtag('js', new Date());
        window.gtag('config', MEASUREMENT_ID, { send_page_view: false });

        const script = document.createElement('script');
        script.async = true;
        script.src = 'https://www.googletagmanager.com/gtag/js?id=' + MEASUREMENT_ID;
        document.head.appendChild(script);
    }

    // Only page metadata; never read forms, student details, or authenticated user data.
    window.gtag('event', 'page_view', {
        send_to: MEASUREMENT_ID,
        page_path: pagePath,
        page_location: pageLocation,
        page_title: PRODUCTION_HOSTNAME + pagePath
    });
}());
