/**
 * Saberistic conversion funnel analytics (Plausible).
 * Loads only when the server injects saberistic-analytics-domain meta.
 * Never sends brief text, email, URLs, or Stripe identifiers.
 */
(function () {
  const UTM_KEY = "saberistic_utm";
  const UTM_PARAMS = [
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
  ];

  const PAGE_EVENTS = {
    "/": { event: "Landing Viewed", step: 1 },
    "/about": { event: "About Viewed" },
    "/services": { event: "Services Viewed" },
    "/case-studies": { event: "Case Studies Viewed" },
    "/insights": { event: "Insights Viewed" },
    "/brief": { event: "Brief Viewed", step: 3 },
    "/brief/success": { event: "Brief Success Viewed", step: 7 },
  };

  const NAV_DESTINATION_EVENTS = {
    "/services": "Nav Services",
    "/case-studies": "Nav Case Studies",
    "/insights": "Nav Insights",
    "/brief": "Nav Diagnostic",
  };

  const domainMeta = document.querySelector(
    'meta[name="saberistic-analytics-domain"]'
  );
  if (!domainMeta || !domainMeta.content) {
    return;
  }

  const domain = domainMeta.content;

  function loadPlausible(onReady) {
    const script = document.createElement("script");
    script.defer = true;
    script.setAttribute("data-domain", domain);
    script.src = "https://plausible.io/js/script.tagged-events.js";
    script.onload = onReady;
    document.head.appendChild(script);
  }

  function captureUtm() {
    try {
      const params = new URLSearchParams(window.location.search);
      const utm = {};
      let found = false;
      UTM_PARAMS.forEach((key) => {
        const value = params.get(key);
        if (value) {
          utm[key] = value;
          found = true;
        }
      });
      if (found) {
        sessionStorage.setItem(UTM_KEY, JSON.stringify(utm));
      }
    } catch (_err) {
      /* sessionStorage unavailable — skip attribution */
    }
  }

  function readUtm() {
    try {
      const raw = sessionStorage.getItem(UTM_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (_err) {
      return {};
    }
  }

  function utmProps() {
    const utm = readUtm();
    const props = {};
    UTM_PARAMS.forEach((key) => {
      if (utm[key]) {
        props[key] = utm[key];
      }
    });
    return props;
  }

  function track(eventName, props) {
    if (typeof window.plausible !== "function") {
      return;
    }
    const finalProps = Object.assign({}, utmProps(), props);
    window.plausible(eventName, { props: finalProps });
  }

  function metaContent(name) {
    const el = document.querySelector('meta[name="' + name + '"]');
    return el && el.content ? el.content : null;
  }

  function trackPageView() {
    const path = window.location.pathname.replace(/\/$/, "") || "/";

    const serverEvent = metaContent("saberistic-analytics-page-event");
    if (serverEvent) {
      const props = { page: path };
      const caseStudySlug = metaContent("saberistic-analytics-case-study-slug");
      if (caseStudySlug) {
        props.case_study_slug = caseStudySlug;
      }
      const articleSlug = metaContent("saberistic-analytics-article-slug");
      if (articleSlug) {
        props.article_slug = articleSlug;
      }
      track(serverEvent, props);
      return;
    }

    const page = PAGE_EVENTS[path];
    if (!page) {
      return;
    }
    const props = { page: path };
    if (page.step) {
      props.funnel_step = page.step;
    }
    track(page.event, props);
  }

  function bindBriefForm() {
    const form = document.getElementById("brief-form");
    if (!form) {
      return;
    }

    let started = false;
    const markStarted = () => {
      if (started) {
        return;
      }
      started = true;
      track("Brief Form Started", { page: "/brief", funnel_step: 4 });
    };

    form.addEventListener("focusin", markStarted, { once: true });
    form.addEventListener("input", markStarted, { once: true });

    if (new URLSearchParams(window.location.search).get("cancelled") === "1") {
      track("Checkout Cancelled", { page: "/brief", funnel_step: 6 });
    }
  }

  function bindContactLinks() {
    document.querySelectorAll('a[href*="linkedin.com/in/saberistic"]').forEach(
      (link) => {
        link.addEventListener("click", () => {
          track("Contact Initiated", {
            page: window.location.pathname,
            contact_channel: "linkedin",
            funnel_step: 8,
          });
        });
      }
    );
  }

  function bindNavLinks() {
    document
      .querySelectorAll(".top-nav a[data-nav-destination]")
      .forEach((link) => {
        link.addEventListener("click", () => {
          const destination = link.getAttribute("data-nav-destination");
          if (!destination) {
            return;
          }
          const eventName = NAV_DESTINATION_EVENTS[destination];
          if (!eventName) {
            return;
          }
          track(eventName, {
            page: window.location.pathname,
            nav_destination: destination,
          });
        });
      });
  }

  captureUtm();

  let initialized = false;
  const initAnalytics = () => {
    if (initialized) {
      return;
    }
    initialized = true;
    trackPageView();
    bindBriefForm();
    bindContactLinks();
    bindNavLinks();
  };

  const start = () => loadPlausible(initAnalytics);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
