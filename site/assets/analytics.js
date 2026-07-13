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
    "/about": { event: "Service Viewed", step: 2 },
    "/brief": { event: "Brief Viewed", step: 3 },
    "/brief/success": { event: "Brief Success Viewed", step: 7 },
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

  function trackPageView() {
    const path = window.location.pathname.replace(/\/$/, "") || "/";
    const page = PAGE_EVENTS[path];
    if (!page) {
      return;
    }
    track(page.event, { page: path, funnel_step: page.step });
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
  };

  const start = () => loadPlausible(initAnalytics);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
