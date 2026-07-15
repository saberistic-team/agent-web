/**
 * Browser-side LinkedIn export ZIP parser for admin import preview.
 * Limits must stay in sync with app/linkedin_export.py (see tests).
 */
(function () {
  "use strict";

  var LIMITS = {
    MAX_COMPRESSED_BYTES: 50 * 1024 * 1024,
    MAX_UNCOMPRESSED_BYTES: 200 * 1024 * 1024,
    MAX_ARCHIVE_ENTRIES: 500,
    MAX_PATH_DEPTH: 3,
    MAX_PATH_LENGTH: 255,
    MAX_CSV_ROWS: 100000,
    MAX_FIELD_LENGTH: 10000,
    MAX_DUPLICATE_SAMPLES: 20,
    MAX_PROPOSED_SAMPLE: 50,
    MAX_IGNORED_SAMPLES: 15,
  };

  var NESTED_ARCHIVE_SUFFIXES = [
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
  ];

  var APPROVED_BASENAMES = {
    "connections.csv": "connections",
    "messages.csv": "messages",
    "invitations.csv": "invitations",
    "company follows.csv": "company_follows",
  };

  var REQUIRED_HEADERS = {
    "connections.csv": ["first name", "last name", "url"],
    "messages.csv": ["conversation id", "from", "to", "date"],
    "invitations.csv": ["from", "to", "sent at"],
    "company follows.csv": ["organization"],
  };

  function normalizeBasename(path) {
    var normalized = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    var parts = normalized.split("/");
    return parts[parts.length - 1].toLowerCase();
  }

  function pathDepth(path) {
    var normalized = path.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    if (!normalized) return 0;
    return normalized.split("/").filter(Boolean).length;
  }

  function validateArchivePath(path) {
    if (!path || path.length > LIMITS.MAX_PATH_LENGTH) {
      return "Archive entry path exceeds length limit";
    }
    if (path.indexOf("\0") !== -1) {
      return "Archive entry path contains null bytes";
    }
    if (/^[/\\]/.test(path) || /^[A-Za-z]:/.test(path)) {
      return "Archive entry path must be relative";
    }
    var parts = path.replace(/\\/g, "/").split("/");
    for (var i = 0; i < parts.length; i += 1) {
      if (parts[i] === "..") {
        return "Archive entry path traversal is not allowed";
      }
    }
    if (pathDepth(path) > LIMITS.MAX_PATH_DEPTH) {
      return "Archive entry path exceeds nesting depth";
    }
    var lower = path.toLowerCase();
    for (var j = 0; j < NESTED_ARCHIVE_SUFFIXES.length; j += 1) {
      if (lower.endsWith(NESTED_ARCHIVE_SUFFIXES[j])) {
        return "Nested archives are not allowed";
      }
    }
    return null;
  }

  function parseCsv(text) {
    var rows = [];
    var row = [];
    var field = "";
    var inQuotes = false;
    for (var i = 0; i < text.length; i += 1) {
      var ch = text.charAt(i);
      if (inQuotes) {
        if (ch === '"') {
          if (text.charAt(i + 1) === '"') {
            field += '"';
            i += 1;
          } else {
            inQuotes = false;
          }
        } else {
          field += ch;
        }
      } else if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        row.push(field);
        field = "";
      } else if (ch === "\r") {
        continue;
      } else if (ch === "\n") {
        row.push(field);
        rows.push(row);
        row = [];
        field = "";
      } else {
        field += ch;
      }
    }
    if (field.length || row.length) {
      row.push(field);
      rows.push(row);
    }
    return rows;
  }

  function normalizeHeaders(headerRow) {
    return headerRow.map(function (cell) {
      return cell.trim().toLowerCase();
    });
  }

  function validateHeaders(headers, required) {
    var headerSet = {};
    headers.forEach(function (h) {
      headerSet[h] = true;
    });
    var missing = [];
    required.forEach(function (req) {
      if (!headerSet[req]) missing.push(req);
    });
    if (missing.length) {
      return "Missing required columns: " + missing.sort().join(", ");
    }
    return null;
  }

  function truncateField(value) {
    if (value.length <= LIMITS.MAX_FIELD_LENGTH) {
      return { value: value, truncated: false };
    }
    return { value: value.slice(0, LIMITS.MAX_FIELD_LENGTH), truncated: true };
  }

  function readCsvRows(rawBytes, requiredHeaders) {
    var warnings = [];
    var decoder = new TextDecoder("utf-8", { fatal: true });
    var text;
    try {
      text = decoder.decode(rawBytes);
    } catch (_err) {
      return { rows: [], rowCount: 0, skipped: 0, errors: ["CSV is not valid UTF-8"] };
    }
    if (text.charCodeAt(0) === 0xfeff) {
      text = text.slice(1);
    }
    var parsed = parseCsv(text);
    if (!parsed.length) {
      return { rows: [], rowCount: 0, skipped: 0, errors: ["CSV is empty"] };
    }
    var headers = normalizeHeaders(parsed[0]);
    var schemaError = validateHeaders(headers, requiredHeaders);
    if (schemaError) {
      return { rows: [], rowCount: 0, skipped: 0, errors: [schemaError] };
    }
    var rows = [];
    var skipped = 0;
    for (var i = 1; i < parsed.length; i += 1) {
      if (i > LIMITS.MAX_CSV_ROWS) {
        warnings.push(
          "Row limit (" + LIMITS.MAX_CSV_ROWS + ") reached; remaining rows ignored"
        );
        break;
      }
      var cells = parsed[i];
      if (!cells.length || cells.every(function (c) { return !c.trim(); })) {
        skipped += 1;
        continue;
      }
      if (cells.length !== headers.length) {
        skipped += 1;
        continue;
      }
      var record = {};
      var truncated = false;
      for (var j = 0; j < headers.length; j += 1) {
        var result = truncateField(cells[j].trim());
        truncated = truncated || result.truncated;
        record[headers[j]] = result.value;
      }
      if (truncated) {
        warnings.push(
          "Row " + (i + 1) + ": one or more fields truncated to " + LIMITS.MAX_FIELD_LENGTH + " chars"
        );
      }
      rows.push(record);
    }
    return { rows: rows, rowCount: rows.length + skipped, skipped: skipped, warnings: warnings };
  }

  function normalizeProfileUrl(value) {
    if (!value) return null;
    var text = value.trim();
    if (!text) return null;
    var urlText = text.indexOf("://") === -1 ? "https://" + text : text;
    var parsed;
    try {
      parsed = new URL(urlText);
    } catch (_err) {
      return null;
    }
    if (!parsed.hostname) return null;
    var host = parsed.hostname.replace(/\.$/, "").toLowerCase();
    if (host.indexOf("www.") === 0) host = host.slice(4);
    var path = parsed.pathname.replace(/\/$/, "") || "";
    return ("https://" + host + path).toLowerCase();
  }

  function normalizeEmail(value) {
    if (!value) return null;
    var email = value.trim().toLowerCase();
    if (!email || email.indexOf("@") === -1 || email.charAt(0) === "@" || email.charAt(email.length - 1) === "@") {
      return null;
    }
    return email;
  }

  function parseConnections(rows) {
    var proposed = [];
    var duplicates = [];
    var seenUrls = {};
    var skipped = 0;
    rows.forEach(function (row) {
      var first = (row["first name"] || "").trim();
      var last = (row["last name"] || "").trim();
      var urlRaw = (row.url || "").trim();
      if (!first && !last && !urlRaw) {
        skipped += 1;
        return;
      }
      var profileUrl = normalizeProfileUrl(urlRaw);
      if (urlRaw && !profileUrl) {
        skipped += 1;
        return;
      }
      var email = normalizeEmail(row["email address"]);
      var name = [first, last].filter(Boolean).join(" ") || "Unknown";
      if (profileUrl) {
        if (seenUrls[profileUrl]) {
          duplicates.push({
            kind: "connection_profile_url",
            value: profileUrl,
            first_row: seenUrls[profileUrl],
          });
        } else {
          seenUrls[profileUrl] = proposed.length + 1;
        }
      }
      if (proposed.length < LIMITS.MAX_PROPOSED_SAMPLE) {
        proposed.push({
          kind: "contact",
          name: name,
          company: (row.company || "").trim() || null,
          title: (row.position || "").trim() || null,
          profile_url: profileUrl,
          email: email,
          connected_on: (row["connected on"] || "").trim() || null,
          source_file: "connections.csv",
        });
      }
    });
    return { proposed: proposed, duplicates: duplicates, skipped: skipped };
  }

  function parseMessages(rows) {
    var conversations = {};
    var skipped = 0;
    rows.forEach(function (row) {
      var convId = (row["conversation id"] || "").trim();
      if (!convId) {
        skipped += 1;
        return;
      }
      conversations[convId] = true;
    });
    return {
      counts: { conversations: Object.keys(conversations).length, messages: rows.length - skipped },
      skipped: skipped,
    };
  }

  function parseInvitations(rows) {
    var proposed = [];
    var skipped = 0;
    rows.forEach(function (row) {
      var sender = (row.from || "").trim();
      var recipient = (row.to || "").trim();
      if (!sender && !recipient) {
        skipped += 1;
        return;
      }
      if (proposed.length < LIMITS.MAX_PROPOSED_SAMPLE) {
        proposed.push({
          kind: "invitation",
          from: sender || null,
          to: recipient || null,
          sent_at: (row["sent at"] || "").trim() || null,
          direction: (row.direction || "").trim() || null,
          source_file: "invitations.csv",
        });
      }
    });
    return { proposed: proposed, skipped: skipped };
  }

  function parseCompanyFollows(rows) {
    var proposed = [];
    var duplicates = [];
    var seenOrgs = {};
    var skipped = 0;
    rows.forEach(function (row) {
      var org = (row.organization || "").trim();
      if (!org) {
        skipped += 1;
        return;
      }
      var key = org.toLowerCase();
      if (seenOrgs[key]) {
        duplicates.push({
          kind: "company_follow",
          value: org,
          first_row: seenOrgs[key],
        });
      } else {
        seenOrgs[key] = proposed.length + 1;
      }
      if (proposed.length < LIMITS.MAX_PROPOSED_SAMPLE) {
        proposed.push({
          kind: "company_follow",
          organization: org,
          followed_on: (row["followed on"] || "").trim() || null,
          source_file: "company follows.csv",
        });
      }
    });
    return { proposed: proposed, duplicates: duplicates, skipped: skipped };
  }

  function parseLinkedInExportZip(arrayBuffer) {
    var preview = {
      ok: false,
      errors: [],
      warnings: [],
      recognized_files: [],
      ignored_file_count: 0,
      ignored_file_samples: [],
      counts: {},
      duplicates: [],
      proposed_changes: [],
      messages_redacted: true,
    };

    if (arrayBuffer.byteLength > LIMITS.MAX_COMPRESSED_BYTES) {
      preview.errors.push(
        "Compressed archive exceeds " + (LIMITS.MAX_COMPRESSED_BYTES / (1024 * 1024)) + " MB limit"
      );
      return preview;
    }

    var entries;
    try {
      entries = fflate.unzipSync(new Uint8Array(arrayBuffer));
    } catch (_err) {
      preview.errors.push("File is not a valid ZIP archive");
      return preview;
    }

    var paths = Object.keys(entries);
    if (paths.length > LIMITS.MAX_ARCHIVE_ENTRIES) {
      preview.errors.push("Archive contains more than " + LIMITS.MAX_ARCHIVE_ENTRIES + " entries");
      return preview;
    }

    var totalUncompressed = 0;
    var approvedPayloads = {};
    var ignoredNames = [];

    for (var i = 0; i < paths.length; i += 1) {
      var path = paths[i];
      var pathError = validateArchivePath(path);
      if (pathError) {
        preview.errors.push(path + ": " + pathError);
        return preview;
      }
      if (path.endsWith("/")) continue;
      var payload = entries[path];
      totalUncompressed += payload.byteLength;
      if (totalUncompressed > LIMITS.MAX_UNCOMPRESSED_BYTES) {
        preview.errors.push(
          "Uncompressed archive exceeds " +
            (LIMITS.MAX_UNCOMPRESSED_BYTES / (1024 * 1024)) +
            " MB limit"
        );
        return preview;
      }
      var basename = normalizeBasename(path);
      if (APPROVED_BASENAMES[basename]) {
        if (approvedPayloads[basename]) {
          preview.warnings.push("Duplicate approved file ignored: " + basename);
          continue;
        }
        approvedPayloads[basename] = payload;
      } else {
        ignoredNames.push(path);
      }
    }

    preview.ignored_file_count = ignoredNames.length;
    preview.ignored_file_samples = ignoredNames.slice(0, LIMITS.MAX_IGNORED_SAMPLES);

    var counts = {
      connections: 0,
      messages: 0,
      conversations: 0,
      invitations: 0,
      company_follows: 0,
    };
    var allProposed = [];
    var allDuplicates = [];

    Object.keys(APPROVED_BASENAMES).forEach(function (basename) {
      if (!approvedPayloads[basename]) return;
      var kind = APPROVED_BASENAMES[basename];
      var csvResult = readCsvRows(approvedPayloads[basename], REQUIRED_HEADERS[basename]);
      if (csvResult.errors && csvResult.errors.length) {
        preview.errors.push(basename + ": " + csvResult.errors[0]);
        return;
      }
      (csvResult.warnings || []).forEach(function (w) {
        preview.warnings.push(w);
      });
      var rows = csvResult.rows;
      preview.recognized_files.push({
        basename: basename,
        kind: kind,
        row_count: csvResult.rowCount,
        valid_rows: rows.length,
        skipped_rows: csvResult.skipped,
      });
      if (kind === "connections") {
        var conn = parseConnections(rows);
        counts.connections = rows.length - conn.skipped;
        allProposed = allProposed.concat(conn.proposed);
        allDuplicates = allDuplicates.concat(conn.duplicates);
      } else if (kind === "messages") {
        var msg = parseMessages(rows);
        counts.messages = msg.counts.messages;
        counts.conversations = msg.counts.conversations;
        if (msg.skipped) {
          preview.warnings.push("messages.csv: skipped " + msg.skipped + " rows without conversation id");
        }
      } else if (kind === "invitations") {
        var inv = parseInvitations(rows);
        counts.invitations = rows.length - inv.skipped;
        allProposed = allProposed.concat(inv.proposed);
      } else if (kind === "company_follows") {
        var follows = parseCompanyFollows(rows);
        counts.company_follows = rows.length - follows.skipped;
        allProposed = allProposed.concat(follows.proposed);
        allDuplicates = allDuplicates.concat(follows.duplicates);
      }
    });

    if (!Object.keys(approvedPayloads).length) {
      preview.errors.push("No recognized LinkedIn export files found in archive");
      return preview;
    }
    if (preview.errors.length) {
      return preview;
    }

    preview.counts = counts;
    preview.proposed_changes = allProposed;
    preview.duplicates = allDuplicates.slice(0, LIMITS.MAX_DUPLICATE_SAMPLES);
    preview.ok = true;
    preview.messages_redacted = true;
    return preview;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderPreviewPanel(preview) {
    var root = document.getElementById("linkedin-import-preview");
    if (!root) return;
    if (!preview.ok) {
      root.innerHTML =
        '<div class="admin-alert admin-alert-error" role="alert"><p class="admin-alert-title">Could not parse export</p><ul>' +
        preview.errors.map(function (e) {
          return "<li>" + escapeHtml(e) + "</li>";
        }).join("") +
        "</ul></div>";
      root.hidden = false;
      return;
    }

    var counts = preview.counts || {};
    var summary =
      '<div class="admin-import-summary">' +
      '<dl class="admin-stat-grid">' +
      "<div><dt>Connections</dt><dd>" + escapeHtml(counts.connections || 0) + "</dd></div>" +
      "<div><dt>Conversations</dt><dd>" + escapeHtml(counts.conversations || 0) + "</dd></div>" +
      "<div><dt>Messages</dt><dd>" + escapeHtml(counts.messages || 0) + " <span class=\"admin-note\">(content not shown)</span></dd></div>" +
      "<div><dt>Invitations</dt><dd>" + escapeHtml(counts.invitations || 0) + "</dd></div>" +
      "<div><dt>Company follows</dt><dd>" + escapeHtml(counts.company_follows || 0) + "</dd></div>" +
      "</dl></div>";

    var recognized =
      '<h2 class="admin-subtitle">Recognized files</h2><ul class="admin-import-file-list">' +
      preview.recognized_files.map(function (f) {
        return (
          "<li><code>" +
          escapeHtml(f.basename) +
          "</code> — " +
          escapeHtml(f.valid_rows) +
          " valid rows (" +
          escapeHtml(f.skipped_rows) +
          " skipped)</li>"
        );
      }).join("") +
      "</ul>";

    var ignored =
      '<h2 class="admin-subtitle">Ignored archive contents</h2>' +
      "<p class=\"admin-note\">" +
      escapeHtml(preview.ignored_file_count) +
      " other file(s) were skipped (logins, security challenges, phones, ads, receipts, etc.).</p>";
    if (preview.ignored_file_samples.length) {
      ignored +=
        "<ul class=\"admin-import-file-list admin-import-muted\">" +
        preview.ignored_file_samples.map(function (name) {
          return "<li><code>" + escapeHtml(name) + "</code></li>";
        }).join("") +
        "</ul>";
    }

    var warningsHtml = "";
    if (preview.warnings.length) {
      warningsHtml =
        '<div class="admin-alert admin-alert-warn" role="status"><p class="admin-alert-title">Validation warnings</p><ul>' +
        preview.warnings.map(function (w) {
          return "<li>" + escapeHtml(w) + "</li>";
        }).join("") +
        "</ul></div>";
    }

    var duplicatesHtml = "";
    if (preview.duplicates.length) {
      duplicatesHtml =
        '<h2 class="admin-subtitle">Duplicates in export</h2><ul class="admin-import-file-list">' +
        preview.duplicates.map(function (d) {
          return (
            "<li>" +
            escapeHtml(d.kind) +
            ": <code>" +
            escapeHtml(d.value) +
            "</code> (first seen row " +
            escapeHtml(d.first_row) +
            ")</li>"
          );
        }).join("") +
        "</ul>";
    }

    var proposedRows = preview.proposed_changes.slice(0, 25).map(function (row) {
      if (row.kind === "contact") {
        return (
          "<tr><td>Contact</td><td>" +
          escapeHtml(row.name) +
          "</td><td>" +
          escapeHtml(row.company || "—") +
          "</td><td>" +
          escapeHtml(row.profile_url || "—") +
          "</td></tr>"
        );
      }
      if (row.kind === "invitation") {
        return (
          "<tr><td>Invitation</td><td>" +
          escapeHtml(row.from || "—") +
          "</td><td>" +
          escapeHtml(row.to || "—") +
          "</td><td>" +
          escapeHtml(row.sent_at || "—") +
          "</td></tr>"
        );
      }
      return (
        "<tr><td>Company follow</td><td>" +
        escapeHtml(row.organization) +
        "</td><td>—</td><td>" +
        escapeHtml(row.followed_on || "—") +
        "</td></tr>"
      );
    }).join("");

    var proposed =
      '<h2 class="admin-subtitle">Proposed import preview</h2>' +
      '<p class="admin-note">Sample rows only — full commit ships in a later issue. Message bodies are never displayed or transmitted.</p>' +
      '<div class="admin-table-wrap"><table class="admin-table"><thead><tr><th>Type</th><th>Primary</th><th>Secondary</th><th>Detail</th></tr></thead><tbody>' +
      (proposedRows || '<tr><td colspan="4">No importable rows detected.</td></tr>') +
      "</tbody></table></div>";

    root.innerHTML =
      '<div class="admin-alert admin-alert-ok" role="status"><p class="admin-alert-title">Export parsed locally</p><p class="admin-note">Nothing has been uploaded or saved. Review the summary below before any future import.</p></div>' +
      summary +
      recognized +
      ignored +
      warningsHtml +
      duplicatesHtml +
      proposed;
    root.hidden = false;
  }

  function initLinkedInImportPage() {
    var input = document.getElementById("linkedin-export-file");
    var status = document.getElementById("linkedin-import-status");
    if (!input) return;

    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      var previewRoot = document.getElementById("linkedin-import-preview");
      if (previewRoot) {
        previewRoot.hidden = true;
        previewRoot.innerHTML = "";
      }
      if (!file) return;
      if (!/\.zip$/i.test(file.name)) {
        if (status) {
          status.textContent = "Please choose a .zip file from your LinkedIn data export.";
          status.className = "admin-import-status admin-import-status-error";
        }
        return;
      }
      if (file.size > LIMITS.MAX_COMPRESSED_BYTES) {
        if (status) {
          status.textContent = "File exceeds the maximum compressed size limit.";
          status.className = "admin-import-status admin-import-status-error";
        }
        return;
      }
      if (status) {
        status.textContent = "Parsing locally in your browser…";
        status.className = "admin-import-status";
      }
      file.arrayBuffer().then(function (buffer) {
        var preview = parseLinkedInExportZip(buffer);
        renderPreviewPanel(preview);
        if (status) {
          status.textContent = preview.ok
            ? "Preview ready — no data left this browser session."
            : "Parsing failed — see errors below.";
          status.className = preview.ok
            ? "admin-import-status admin-import-status-ok"
            : "admin-import-status admin-import-status-error";
        }
      }).catch(function () {
        if (status) {
          status.textContent = "Could not read the selected file.";
          status.className = "admin-import-status admin-import-status-error";
        }
      });
    });
  }

  window.LinkedInExportParser = {
    LIMITS: LIMITS,
    parseLinkedInExportZip: parseLinkedInExportZip,
    renderPreviewPanel: renderPreviewPanel,
    initLinkedInImportPage: initLinkedInImportPage,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initLinkedInImportPage);
  } else {
    initLinkedInImportPage();
  }
})();
