/**
 * Saberistic first-party analytics (schema v1).
 * Loads only when the server injects saberistic-first-party-analytics meta.
 * Honors Do Not Track / Global Privacy Control; never blocks navigation.
 */
(function () {
  const ENABLED_META = "saberistic-first-party-analytics";
  const SESSION_KEY = "saberistic_analytics_sid";
  const SESSION_ROTATE_HEADER = "x-analytics-session-rotate";
  const UTM_KEY = "saberistic_utm";
  const UTM_PARAMS = [
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
  ];
  const SCHEMA_VERSION = "1.0.0";
  const INGEST_PATH = "/api/events";

  const PAGE_EVENTS = {
    "/": { event: "Landing Viewed", step: 1, pathClass: "landing" },
    "/about": { event: "About Viewed", pathClass: "about" },
    "/services": { event: "Services Viewed", pathClass: "services" },
    "/case-studies": { event: "Case Studies Viewed", pathClass: "case_studies" },
    "/insights": { event: "Insights Viewed", pathClass: "insights" },
    "/brief": { event: "Brief Viewed", step: 3, pathClass: "brief" },
    "/brief/success": { event: "Brief Success Viewed", step: 7, pathClass: "brief_success" },
  };

  const NAV_DESTINATION_EVENTS = {
    "/services": "Nav Services",
    "/case-studies": "Nav Case Studies",
    "/insights": "Nav Insights",
    "/brief": "Nav Diagnostic",
  };

  const enabledMeta = document.querySelector('meta[name="' + ENABLED_META + '"]');
  if (!enabledMeta || !enabledMeta.content) {
    return;
  }

  function privacyBlocked() {
    try {
      if (navigator.doNotTrack === "1" || navigator.doNotTrack === "yes") {
        return true;
      }
      if (navigator.globalPrivacyControl === true) {
        return true;
      }
    } catch (_err) {
      /* ignore */
    }
    return false;
  }

  function classifyReferrer() {
    try {
      if (!document.referrer) {
        return "direct";
      }
      const ref = new URL(document.referrer);
      if (ref.origin === window.location.origin) {
        return "internal";
      }
      const host = ref.hostname.toLowerCase();
      if (host.includes("google.") || host.includes("bing.") || host.includes("duckduckgo.")) {
        return "search";
      }
      if (host.includes("linkedin.") || host.includes("twitter.") || host === "x.com") {
        return "social";
      }
      if (host.includes("mail.") || host.includes("outlook.") || host.includes("gmail.")) {
        return "email";
      }
      return "unknown_external";
    } catch (_err) {
      return "direct";
    }
  }

  function classifyPath(pathname) {
    const path = (pathname || "/").replace(/\/$/, "") || "/";
    if (path === "/") return "landing";
    if (path === "/about") return "about";
    if (path === "/services") return "services";
    if (path === "/case-studies") return "case_studies";
    if (path.startsWith("/work/")) return "case_study";
    if (path === "/insights") return "insights";
    if (path.startsWith("/insights/")) return "insight";
    if (path === "/brief") return "brief";
    if (path === "/brief/success") return "brief_success";
    return "unknown";
  }

  function randomUuid() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      const r = (Math.random() * 16) | 0;
      const v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function readSessionId() {
    try {
      return sessionStorage.getItem(SESSION_KEY);
    } catch (_err) {
      return null;
    }
  }

  function writeSessionId(value) {
    try {
      sessionStorage.setItem(SESSION_KEY, value);
    } catch (_err) {
      /* sessionStorage unavailable */
    }
  }

  function ensureSessionId() {
    let sessionId = readSessionId();
    if (!sessionId) {
      sessionId = randomUuid();
      writeSessionId(sessionId);
    }
    return sessionId;
  }

  function captureUtm() {
    try {
      const params = new URLSearchParams(window.location.search);
      const utm = {};
      let found = false;
      UTM_PARAMS.forEach(function (key) {
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
      /* skip attribution */
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

  function metaContent(name) {
    const el = document.querySelector('meta[name="' + name + '"]');
    return el && el.content ? el.content : null;
  }

  function buildPayload(eventName, props, pathClass) {
    const utm = readUtm();
    const attribution = {};
    UTM_PARAMS.forEach(function (key) {
      if (utm[key]) {
        attribution[key] = utm[key];
      }
    });
    return {
      idempotency_key: randomUuid(),
      event_name: eventName,
      schema_version: SCHEMA_VERSION,
      occurred_at: new Date().toISOString(),
      anonymous_session_id: ensureSessionId(),
      path_class: pathClass || classifyPath(window.location.pathname),
      referrer_class: classifyReferrer(),
      attribution: attribution,
      properties: props || {},
      consent_state: privacyBlocked() ? "declined" : "implicit_analytics",
    };
  }

  function deliver(body) {
    if (privacyBlocked()) {
      return;
    }
    const json = JSON.stringify(body);
    try {
      if (navigator.sendBeacon) {
        const blob = new Blob([json], { type: "application/json" });
        if (navigator.sendBeacon(INGEST_PATH, blob)) {
          return;
        }
      }
    } catch (_err) {
      /* fall through to fetch */
    }
    try {
      fetch(INGEST_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: json,
        keepalive: true,
        credentials: "same-origin",
      })
        .then(function (response) {
          if (response.headers.get(SESSION_ROTATE_HEADER) === "1") {
            writeSessionId(randomUuid());
          }
        })
        .catch(function () {
          /* non-blocking */
        });
    } catch (_err) {
      /* analytics unavailable must not break the page */
    }
  }

  function track(eventName, props, pathClass) {
    try {
      deliver(buildPayload(eventName, props, pathClass));
    } catch (_err) {
      /* non-blocking */
    }
  }

  function trackPageView() {
    const path = window.location.pathname.replace(/\/$/, "") || "/";

    const serverEvent = metaContent("saberistic-first-party-page-event");
    if (serverEvent) {
      const props = { page: path };
      const caseStudySlug = metaContent("saberistic-first-party-case-study-slug");
      if (caseStudySlug) {
        props.case_study_slug = caseStudySlug;
      }
      const articleSlug = metaContent("saberistic-first-party-article-slug");
      if (articleSlug) {
        props.article_slug = articleSlug;
      }
      track(serverEvent, props, classifyPath(path));
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
    track(page.event, props, page.pathClass);
  }

  function bindBriefForm() {
    const form = document.getElementById("brief-form");
    if (!form) {
      return;
    }

    let started = false;
    const markStarted = function () {
      if (started) {
        return;
      }
      started = true;
      track("Brief Form Started", { page: "/brief", funnel_step: 4 }, "brief");
    };

    form.addEventListener("focusin", markStarted, { once: true });
    form.addEventListener("input", markStarted, { once: true });

    if (new URLSearchParams(window.location.search).get("cancelled") === "1") {
      track("Checkout Cancelled", { page: "/brief", funnel_step: 6 }, "brief");
    }
  }

  function bindContactLinks() {
    document.querySelectorAll('a[href*="linkedin.com/in/saberistic"]').forEach(
      function (link) {
        link.addEventListener("click", function () {
          track(
            "Contact Initiated",
            {
              page: window.location.pathname,
              contact_channel: "linkedin",
              funnel_step: 8,
            },
            classifyPath(window.location.pathname)
          );
        });
      }
    );
  }

  function bindNavLinks() {
    document.querySelectorAll(".top-nav a[data-nav-destination]").forEach(function (link) {
      link.addEventListener("click", function () {
        const destination = link.getAttribute("data-nav-destination");
        if (!destination) {
          return;
        }
        const eventName = NAV_DESTINATION_EVENTS[destination];
        if (!eventName) {
          return;
        }
        track(
          eventName,
          {
            page: window.location.pathname,
            nav_destination: destination,
          },
          classifyPath(window.location.pathname)
        );
      });
    });
  }

  captureUtm();

  let initialized = false;
  const initAnalytics = function () {
    if (initialized) {
      return;
    }
    initialized = true;
    trackPageView();
    bindBriefForm();
    bindContactLinks();
    bindNavLinks();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAnalytics);
  } else {
    initAnalytics();
  }
})();
