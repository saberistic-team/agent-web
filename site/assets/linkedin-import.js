/**
 * LinkedIn data-export ZIP preview (admin-only, browser-local).
 * Parses approved CSVs only; never uploads the archive or message bodies.
 */
(function () {
  "use strict";

  var LIMITS = {
    maxCompressedBytes: 52428800,
    maxUncompressedBytes: 209715200,
    maxZipEntries: 500,
    maxCsvRows: 50000,
    maxFieldLength: 10000,
    maxPathLength: 512,
    maxPreambleScanLines: 20,
    approvedBasenames: [
      "connections.csv",
      "messages.csv",
      "invitations.csv",
      "company follows.csv",
    ],
  };

  var NESTED_SUFFIXES = [".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"];
  var IGNORED_HINTS = [
    "logins", "login", "phone", "security", "challenge", "job", "ads", "ad ",
    "verification", "receipt", "email addresses", "skills", "positions",
    "education", "certifications", "recommendations", "rich_media", "profile",
    "registration", "saved", "search", "endorsement", "language", "learning",
    "hashtag", "instant", "member", "organization", "project", "publication",
    "volunteer",
  ];
  var REQUIRED_TOKENS = {
    "connections.csv": ["first name", "url"],
    "messages.csv": ["conversation", "date"],
    "invitations.csv": ["from", "sent"],
    "company follows.csv": ["organization", "followed"],
  };

  var form = document.getElementById("linkedin-import-form");
  var input = document.getElementById("linkedin-export-zip");
  var statusEl = document.getElementById("linkedin-import-status");
  var previewEl = document.getElementById("linkedin-import-preview");
  var limitsNode = document.getElementById("linkedin-import-limits");

  if (!form || !input || !statusEl || !previewEl) {
    return;
  }

  if (limitsNode && limitsNode.textContent) {
    try {
      LIMITS = Object.assign(LIMITS, JSON.parse(limitsNode.textContent));
    } catch (_err) {
      /* keep defaults */
    }
  }

  var approvedSet = {};
  (LIMITS.approvedBasenames || []).forEach(function (name) {
    approvedSet[name.toLowerCase()] = true;
  });

  input.addEventListener("change", function () {
    previewEl.hidden = true;
    previewEl.textContent = "";
    statusEl.textContent = "";
    statusEl.className = "linkedin-import-status";

    var file = input.files && input.files[0];
    if (!file) {
      return;
    }

    statusEl.textContent = "Parsing export locally…";
    statusEl.className = "linkedin-import-status linkedin-import-status--busy";

    readAndParse(file)
      .then(function (result) {
        renderResult(result);
      })
      .catch(function (err) {
        statusEl.textContent = "Parse failed: " + String(err && err.message ? err.message : err);
        statusEl.className = "linkedin-import-status linkedin-import-status--error";
      });
  });

  function readAndParse(file) {
    if (file.size > LIMITS.maxCompressedBytes) {
      return Promise.reject(new Error("Archive exceeds compressed size limit."));
    }
    return file.arrayBuffer().then(function (buffer) {
      return parseZip(new Uint8Array(buffer));
    });
  }

  function basename(path) {
    var normalized = String(path).replace(/\\/g, "/");
    var parts = normalized.split("/");
    return parts[parts.length - 1].trim().toLowerCase();
  }

  function isSafePath(path) {
    var normalized = String(path).replace(/\\/g, "/").trim();
    if (!normalized || normalized.length > LIMITS.maxPathLength) {
      return false;
    }
    if (normalized.charAt(0) === "/" || /^[A-Za-z]:/.test(normalized)) {
      return false;
    }
    var parts = normalized.split("/");
    for (var i = 0; i < parts.length; i += 1) {
      if (!parts[i] || parts[i] === "." || parts[i] === "..") {
        return false;
      }
    }
    return true;
  }

  function isNestedArchive(path) {
    var base = basename(path);
    for (var i = 0; i < NESTED_SUFFIXES.length; i += 1) {
      if (base.endsWith(NESTED_SUFFIXES[i])) {
        return true;
      }
    }
    return false;
  }

  function looksIgnored(path) {
    var base = basename(path);
    if (approvedSet[base]) {
      return false;
    }
    var lower = base.toLowerCase();
    for (var i = 0; i < IGNORED_HINTS.length; i += 1) {
      if (lower.indexOf(IGNORED_HINTS[i]) !== -1) {
        return true;
      }
    }
    return true;
  }

  function parseZip(data) {
    var entries = readZipEntries(data);
    if (entries.length > LIMITS.maxZipEntries) {
      return Promise.reject(new Error("Archive contains too many entries."));
    }

    var totalUncompressed = 0;
    var approvedPaths = {};
    var ignored = [];

    for (var i = 0; i < entries.length; i += 1) {
      var scanEntry = entries[i];
      var normalizedName = String(scanEntry.name).replace(/\\/g, "/");
      var isDirEntry = normalizedName.charAt(normalizedName.length - 1) === "/";
      // Validate the path without its trailing separator so a safe explicit
      // directory entry (e.g. "LinkedIn Export/") is not rejected because
      // isSafePath() sees a trailing empty path segment.
      var pathToValidate = isDirEntry ? normalizedName.slice(0, -1) : normalizedName;
      if (!isSafePath(pathToValidate)) {
        return Promise.reject(new Error("Unsafe archive path rejected: " + scanEntry.name));
      }
      if (isDirEntry) {
        continue;
      }
      if (isNestedArchive(scanEntry.name)) {
        return Promise.reject(new Error("Nested archives are not allowed: " + scanEntry.name));
      }
      totalUncompressed += scanEntry.uncompressedSize;
      if (totalUncompressed > LIMITS.maxUncompressedBytes) {
        return Promise.reject(new Error("Uncompressed contents exceed size limit."));
      }
      var scanBase = basename(scanEntry.name);
      if (approvedSet[scanBase]) {
        if (approvedPaths[scanBase]) {
          return Promise.reject(
            new Error(
              "Duplicate approved file '" +
                scanBase +
                "' found at multiple paths: " +
                approvedPaths[scanBase].name +
                " and " +
                scanEntry.name
            )
          );
        }
        approvedPaths[scanBase] = scanEntry;
      } else {
        ignored.push(scanEntry.name);
      }
    }

    var bases = Object.keys(approvedPaths).sort();
    return bases
      .reduce(function (chain, base) {
        return chain.then(function (state) {
          var entry = approvedPaths[base];
          return inflateEntry(data, entry)
            .then(function (raw) {
              if (raw.length > LIMITS.maxUncompressedBytes) {
                state.errors.push(base + ": uncompressed file too large");
                return state;
              }
              var parsed;
              try {
                parsed = parseCsvBytes(raw, base);
              } catch (err) {
                state.errors.push(String(err.message || err));
                return state;
              }
              var fileWarnings = parsed.warnings.slice();
              var validRows = 0;
              var skippedRows = 0;

              if (base === "connections.csv") {
                var conn = summarizeConnections(parsed.rows);
                validRows = conn.valid;
                skippedRows = parsed.rows.length - conn.valid;
                state.counts.connections = conn.valid;
                state.duplicateUrls = state.duplicateUrls.concat(conn.duplicates);
                fileWarnings = fileWarnings.concat(conn.warnings);
              } else if (base === "messages.csv") {
                var msg = summarizeMessages(parsed.rows);
                validRows = msg.valid;
                skippedRows = parsed.rows.length - msg.valid;
                state.counts.message_threads = msg.threads;
                state.counts.messages = msg.valid;
                fileWarnings = fileWarnings.concat(msg.warnings);
              } else if (base === "invitations.csv") {
                validRows = countNonEmptyRows(parsed.rows);
                skippedRows = parsed.rows.length - validRows;
                state.counts.invitations = validRows;
              } else if (base === "company follows.csv") {
                var fol = summarizeCompanyFollows(parsed.rows);
                validRows = fol.valid;
                skippedRows = parsed.rows.length - fol.valid;
                state.counts.company_follows = fol.valid;
                fileWarnings = fileWarnings.concat(fol.warnings);
              }

              state.summaries.push({
                basename: base,
                archivePath: entry.name,
                rowCount: parsed.rows.length,
                validRows: validRows,
                skippedRows: skippedRows,
                warnings: fileWarnings,
              });
              state.warnings = state.warnings.concat(fileWarnings);
              return state;
            })
            .catch(function (err) {
              state.errors.push("Could not read " + entry.name + ": " + err.message);
              return state;
            });
        });
      }, Promise.resolve({
        summaries: [],
        counts: {
          connections: 0,
          message_threads: 0,
          messages: 0,
          invitations: 0,
          company_follows: 0,
        },
        duplicateUrls: [],
        warnings: [],
        errors: [],
      }))
      .then(function (state) {
        if (state.errors.length) {
          return {
            ok: false,
            errors: state.errors,
            warnings: state.warnings,
            ignoredFiles: uniqueSorted(ignored),
            files: state.summaries,
          };
        }
        if (!state.summaries.length) {
          state.warnings.push("No approved CSV files found in archive.");
        }
        return {
          ok: true,
          errors: [],
          warnings: state.warnings,
          ignoredFiles: uniqueSorted(ignored),
          files: state.summaries,
          connectionCount: state.counts.connections,
          messageThreadCount: state.counts.message_threads,
          messageRowCount: state.counts.messages,
          invitationCount: state.counts.invitations,
          companyFollowCount: state.counts.company_follows,
          duplicateProfileUrls: uniqueSorted(state.duplicateUrls),
          proposedChanges: {
            new_connections: state.counts.connections,
            message_threads: state.counts.message_threads,
            invitations: state.counts.invitations,
            company_follows: state.counts.company_follows,
          },
        };
      });
  }

  function readZipEntries(data) {
    var eocd = findEndOfCentralDirectory(data);
    if (!eocd) {
      throw new Error("File is not a valid ZIP archive.");
    }
    var entryCount = readUint16(data, eocd + 10);
    var centralOffset = readUint32(data, eocd + 16);
    var entries = [];
    var offset = centralOffset;
    var centralHeaderLength = 46;

    for (var i = 0; i < entryCount; i += 1) {
      if (
        offset < 0 ||
        offset + centralHeaderLength > data.length ||
        readUint32(data, offset) !== 0x02014b50
      ) {
        throw new Error("Malformed ZIP central directory.");
      }
      var generalPurposeFlag = readUint16(data, offset + 8);
      var compression = readUint16(data, offset + 10);
      var compressedSize = readUint32(data, offset + 20);
      var uncompressedSize = readUint32(data, offset + 24);
      var nameLen = readUint16(data, offset + 28);
      var extraLen = readUint16(data, offset + 30);
      var commentLen = readUint16(data, offset + 32);
      var localHeaderOffset = readUint32(data, offset + 42);
      var nameBytes = data.subarray(offset + 46, offset + 46 + nameLen);
      var name = new TextDecoder("utf-8").decode(nameBytes);
      entries.push({
        name: name,
        generalPurposeFlag: generalPurposeFlag,
        compression: compression,
        // Authoritative sizes from the central directory. The local file
        // header may report 0 (general-purpose flag bit 3 / data
        // descriptor) — never trust it over this value.
        compressedSize: compressedSize,
        uncompressedSize: uncompressedSize,
        localHeaderOffset: localHeaderOffset,
      });
      offset += 46 + nameLen + extraLen + commentLen;
    }
    return entries;
  }

  function findEndOfCentralDirectory(data) {
    var min = Math.max(0, data.length - 65557);
    for (var i = data.length - 22; i >= min; i -= 1) {
      if (
        readUint32(data, i) === 0x06054b50 &&
        readUint16(data, i + 20) <= data.length - i - 22
      ) {
        return i;
      }
    }
    return null;
  }

  var LOCAL_HEADER_FIXED_LENGTH = 30;

  function inflateEntry(data, entry) {
    var off = entry.localHeaderOffset;
    if (
      typeof off !== "number" ||
      off < 0 ||
      off + LOCAL_HEADER_FIXED_LENGTH > data.length
    ) {
      return Promise.reject(
        new Error("Invalid local file header offset for entry: " + entry.name)
      );
    }
    if (readUint32(data, off) !== 0x04034b50) {
      return Promise.reject(new Error("Malformed local file header for entry: " + entry.name));
    }

    var localGeneralPurposeFlag = readUint16(data, off + 6);
    // Check both local and central-directory flags — a mismatched pair could
    // otherwise slip an encrypted entry past whichever header is trusted.
    if (
      (localGeneralPurposeFlag & 0x0001) !== 0 ||
      ((entry.generalPurposeFlag || 0) & 0x0001) !== 0
    ) {
      return Promise.reject(
        new Error("Encrypted archive entries are not supported: " + entry.name)
      );
    }

    var localCompression = readUint16(data, off + 8);
    if (localCompression !== entry.compression) {
      return Promise.reject(
        new Error(
          "Compression method mismatch between local and central directory for entry: " +
            entry.name
        )
      );
    }

    var nameLen = readUint16(data, off + 26);
    var extraLen = readUint16(data, off + 28);
    var start = off + LOCAL_HEADER_FIXED_LENGTH + nameLen + extraLen;
    // Authoritative size from the central directory — never the local
    // header's placeholder (often 0 when general-purpose flag bit 3 / a
    // trailing data descriptor is used).
    var compressedSize = entry.compressedSize;
    var end = start + compressedSize;

    if (
      typeof compressedSize !== "number" ||
      compressedSize < 0 ||
      start < off + LOCAL_HEADER_FIXED_LENGTH ||
      start > data.length ||
      end < start ||
      end > data.length
    ) {
      return Promise.reject(
        new Error("Truncated or out-of-bounds compressed data for entry: " + entry.name)
      );
    }

    var compressed = data.subarray(start, end);

    if (localCompression === 0) {
      return Promise.resolve(compressed);
    }
    if (localCompression === 8) {
      return inflateDeflateRaw(compressed);
    }
    return Promise.reject(
      new Error("Unsupported compression method for entry " + entry.name + ": " + localCompression)
    );
  }

  function inflateDeflateRaw(compressed) {
    if (typeof DecompressionStream === "undefined") {
      return Promise.reject(
        new Error("Browser cannot decompress this export (missing DecompressionStream).")
      );
    }
    return Promise.resolve()
      .then(function () {
        var stream = new Blob([compressed])
          .stream()
          .pipeThrough(new DecompressionStream("deflate-raw"));
        return new Response(stream).arrayBuffer();
      })
      .then(function (buf) {
        return new Uint8Array(buf);
      })
      .catch(function (err) {
        throw new Error(
          "Failed to decompress archive entry (deflate error): " +
            (err && err.message ? err.message : String(err))
        );
      });
  }

  function countCsvFields(line) {
    if (!line.trim()) {
      return 0;
    }
    return parseCsvLine(line).length;
  }

  function findHeaderLineIndex(lines) {
    var limit = Math.min(lines.length, LIMITS.maxPreambleScanLines || 20);
    for (var i = 0; i < limit; i += 1) {
      if (!lines[i].trim()) {
        continue;
      }
      if (countCsvFields(lines[i]) > 1) {
        return i;
      }
    }
    return -1;
  }

  function parseCsvBytes(raw, basenameKey) {
    var text = new TextDecoder("utf-8", { fatal: false }).decode(raw);
    if (text.indexOf("\u0000") !== -1) {
      throw new Error(basenameKey + ": binary content is not valid CSV");
    }
    if (text.charCodeAt(0) === 0xfeff) {
      text = text.slice(1);
    }
    var lines = splitCsvLines(text);
    if (!lines.length) {
      throw new Error(basenameKey + ": missing CSV header row");
    }
    var headerIndex = findHeaderLineIndex(lines);
    if (headerIndex < 0) {
      throw new Error(basenameKey + ": missing CSV header row");
    }
    var headers = parseCsvLine(lines[headerIndex]);
    if (!headers.length) {
      throw new Error(basenameKey + ": missing CSV header row");
    }
    var warnings = [];
    var headerLower = headers.map(function (h) {
      return h.trim().toLowerCase();
    });
    var required = REQUIRED_TOKENS[basenameKey] || [];
    required.forEach(function (token) {
      var found = headerLower.some(function (h) {
        return h.indexOf(token) !== -1;
      });
      if (!found) {
        warnings.push(basenameKey + ": unexpected schema (missing '" + token + "' column)");
      }
    });

    var rows = [];
    var dataRowCount = 0;
    for (var i = headerIndex + 1; i < lines.length; i += 1) {
      if (!lines[i].trim()) {
        continue;
      }
      dataRowCount += 1;
      if (dataRowCount > LIMITS.maxCsvRows) {
        warnings.push(basenameKey + ": truncated at " + LIMITS.maxCsvRows.toLocaleString() + " rows");
        break;
      }
      var values = parseCsvLine(lines[i]);
      var row = {};
      var nonEmpty = false;
      headers.forEach(function (header, idx) {
        var cell = values[idx] !== undefined ? String(values[idx]) : "";
        if (cell.length > LIMITS.maxFieldLength) {
          warnings.push(
            basenameKey + ": row " + (i + 1) + " field '" + header + "' exceeds max length; truncated"
          );
          cell = cell.slice(0, LIMITS.maxFieldLength);
        }
        if (cell) {
          nonEmpty = true;
        }
        row[header.trim()] = cell;
      });
      if (nonEmpty) {
        rows.push(row);
      }
    }
    return { rows: rows, warnings: warnings };
  }

  function splitCsvLines(text) {
    var lines = [];
    var current = "";
    var inQuotes = false;
    for (var i = 0; i < text.length; i += 1) {
      var ch = text[i];
      if (ch === '"') {
        if (inQuotes && text[i + 1] === '"') {
          current += '"';
          i += 1;
        } else {
          inQuotes = !inQuotes;
          current += ch;
        }
      } else if ((ch === "\n" || ch === "\r") && !inQuotes) {
        if (ch === "\r" && text[i + 1] === "\n") {
          i += 1;
        }
        lines.push(current);
        current = "";
      } else {
        current += ch;
      }
    }
    if (current.length) {
      lines.push(current);
    }
    return lines;
  }

  function parseCsvLine(line) {
    var out = [];
    var current = "";
    var inQuotes = false;
    for (var i = 0; i < line.length; i += 1) {
      var ch = line[i];
      if (ch === '"') {
        if (inQuotes && line[i + 1] === '"') {
          current += '"';
          i += 1;
        } else {
          inQuotes = !inQuotes;
        }
      } else if (ch === "," && !inQuotes) {
        out.push(current);
        current = "";
      } else {
        current += ch;
      }
    }
    out.push(current);
    return out;
  }

  function normalizeProfileUrl(value) {
    var text = String(value || "").trim();
    if (!text) {
      return null;
    }
    var urlText = text.indexOf("://") === -1 ? "https://" + text : text;
    try {
      var parsed = new URL(urlText);
      var host = parsed.hostname.replace(/\.$/, "").toLowerCase();
      if (host.indexOf("www.") === 0) {
        host = host.slice(4);
      }
      var path = parsed.pathname.replace(/\/$/, "") || "";
      return ("https://" + host + path).toLowerCase();
    } catch (_err) {
      return null;
    }
  }

  function summarizeConnections(rows) {
    var valid = 0;
    var seen = {};
    var duplicates = [];
    var warnings = [];
    rows.forEach(function (row) {
      var url = null;
      Object.keys(row).forEach(function (key) {
        var lower = key.toLowerCase();
        if ((lower === "url" || lower === "profile url" || lower === "linkedin url") && row[key]) {
          url = normalizeProfileUrl(row[key]);
        }
      });
      if (!url) {
        return;
      }
      valid += 1;
      seen[url] = (seen[url] || 0) + 1;
      if (seen[url] === 2) {
        duplicates.push(url);
      }
    });
    if (!valid && rows.length) {
      warnings.push("Connections.csv: no rows with a recognizable profile URL");
    }
    return { valid: valid, duplicates: duplicates, warnings: warnings };
  }

  function summarizeMessages(rows) {
    var threads = {};
    var valid = 0;
    var warnings = [];
    rows.forEach(function (row) {
      var convId = "";
      Object.keys(row).forEach(function (key) {
        if (key.toLowerCase().indexOf("conversation") !== -1 && key.toLowerCase().indexOf("id") !== -1) {
          convId = String(row[key] || "").trim();
        }
      });
      if (convId) {
        threads[convId] = true;
        valid += 1;
      }
    });
    if (rows.length && !valid) {
      warnings.push("messages.csv: no rows with a conversation id");
    }
    return { threads: Object.keys(threads).length, valid: valid, warnings: warnings };
  }

  function summarizeCompanyFollows(rows) {
    var valid = 0;
    var warnings = [];
    rows.forEach(function (row) {
      var org = "";
      Object.keys(row).forEach(function (key) {
        var lower = key.toLowerCase();
        if (lower.indexOf("organization") !== -1 || lower === "company") {
          org = String(row[key] || "").trim();
        }
      });
      if (org) {
        valid += 1;
      }
    });
    if (rows.length && !valid) {
      warnings.push("Company Follows.csv: no rows with an organization name");
    }
    return { valid: valid, warnings: warnings };
  }

  function countNonEmptyRows(rows) {
    var count = 0;
    rows.forEach(function (row) {
      var any = Object.keys(row).some(function (key) {
        return String(row[key] || "").trim();
      });
      if (any) {
        count += 1;
      }
    });
    return count;
  }

  function uniqueSorted(items) {
    var map = {};
    items.forEach(function (item) {
      map[item] = true;
    });
    return Object.keys(map).sort();
  }

  function readUint16(data, offset) {
    return data[offset] | (data[offset + 1] << 8);
  }

  function readUint32(data, offset) {
    return (
      (data[offset] |
        (data[offset + 1] << 8) |
        (data[offset + 2] << 16) |
        (data[offset + 3] << 24)) >>>
      0
    );
  }

  function renderResult(result) {
    statusEl.className = "linkedin-import-status";
    if (!result.ok) {
      statusEl.textContent = result.errors.join(" ");
      statusEl.className = "linkedin-import-status linkedin-import-status--error";
    } else {
      statusEl.textContent = "Preview ready — nothing uploaded.";
      statusEl.className = "linkedin-import-status linkedin-import-status--ok";
    }

    previewEl.hidden = false;
    previewEl.textContent = "";

    var title = document.createElement("h2");
    title.className = "admin-section-title";
    title.textContent = "Import preview";
    previewEl.appendChild(title);

    if (result.errors && result.errors.length) {
      appendList(previewEl, "Errors", result.errors, "linkedin-import-errors");
    }
    if (result.warnings && result.warnings.length) {
      appendList(previewEl, "Validation warnings", result.warnings, "linkedin-import-warnings");
    }

    var stats = document.createElement("dl");
    stats.className = "admin-stat-row linkedin-import-stats";
    appendStat(stats, "Connections", result.connectionCount || 0);
    appendStat(stats, "Message threads", result.messageThreadCount || 0);
    appendStat(stats, "Invitations", result.invitationCount || 0);
    appendStat(stats, "Company follows", result.companyFollowCount || 0);
    previewEl.appendChild(stats);

    if (result.proposedChanges) {
      var proposed = document.createElement("div");
      proposed.className = "linkedin-import-proposed";
      proposed.innerHTML =
        "<h3 class=\"admin-section-title\">Proposed changes (preview only)</h3>" +
        "<p class=\"admin-note\">Counts below reflect what a future import step could create. " +
        "Message bodies are counted but never shown or uploaded.</p>" +
        "<ul class=\"linkedin-import-proposed-list\">" +
        "<li><strong>New connections:</strong> " + esc(String(result.proposedChanges.new_connections || 0)) + "</li>" +
        "<li><strong>Message threads:</strong> " + esc(String(result.proposedChanges.message_threads || 0)) + "</li>" +
        "<li><strong>Invitations:</strong> " + esc(String(result.proposedChanges.invitations || 0)) + "</li>" +
        "<li><strong>Company follows:</strong> " + esc(String(result.proposedChanges.company_follows || 0)) + "</li>" +
        "</ul>";
      previewEl.appendChild(proposed);
    }

    if (result.duplicateProfileUrls && result.duplicateProfileUrls.length) {
      appendList(
        previewEl,
        "Duplicate profile URLs in export",
        result.duplicateProfileUrls,
        "linkedin-import-duplicates"
      );
    }

    if (result.files && result.files.length) {
      var filesTitle = document.createElement("h3");
      filesTitle.className = "admin-section-title";
      filesTitle.textContent = "Recognized files";
      previewEl.appendChild(filesTitle);

      var tableWrap = document.createElement("div");
      tableWrap.className = "admin-table-wrap";
      var table = document.createElement("table");
      table.className = "admin-table";
      table.innerHTML =
        "<thead><tr><th>File</th><th>Rows</th><th>Valid</th><th>Skipped</th><th>Path in archive</th></tr></thead>";
      var tbody = document.createElement("tbody");
      result.files.forEach(function (file) {
        var tr = document.createElement("tr");
        tr.innerHTML =
          "<td>" + esc(file.basename) + "</td>" +
          "<td>" + esc(String(file.rowCount)) + "</td>" +
          "<td>" + esc(String(file.validRows)) + "</td>" +
          "<td>" + esc(String(file.skippedRows)) + "</td>" +
          "<td>" + esc(file.archivePath) + "</td>";
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      tableWrap.appendChild(table);
      previewEl.appendChild(tableWrap);
    }

    if (result.ignoredFiles && result.ignoredFiles.length) {
      var ignoredTitle = document.createElement("h3");
      ignoredTitle.className = "admin-section-title";
      ignoredTitle.textContent = "Ignored archive entries (" + result.ignoredFiles.length + ")";
      previewEl.appendChild(ignoredTitle);
      var ignoredNote = document.createElement("p");
      ignoredNote.className = "admin-note";
      ignoredNote.textContent =
        "These paths were skipped and are not parsed or transmitted.";
      previewEl.appendChild(ignoredNote);
      appendList(previewEl, "", result.ignoredFiles.slice(0, 12), "linkedin-import-ignored");
      if (result.ignoredFiles.length > 12) {
        var more = document.createElement("p");
        more.className = "admin-note";
        more.textContent = "+" + (result.ignoredFiles.length - 12) + " more ignored entries.";
        previewEl.appendChild(more);
      }
    }
  }

  function appendStat(dl, label, value) {
    var div = document.createElement("div");
    var dt = document.createElement("dt");
    dt.textContent = label;
    var dd = document.createElement("dd");
    dd.textContent = String(value);
    div.appendChild(dt);
    div.appendChild(dd);
    dl.appendChild(div);
  }

  function appendList(parent, title, items, className) {
    if (title) {
      var heading = document.createElement("h3");
      heading.className = "admin-section-title";
      heading.textContent = title;
      parent.appendChild(heading);
    }
    var ul = document.createElement("ul");
    ul.className = className;
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = String(item);
      ul.appendChild(li);
    });
    parent.appendChild(ul);
  }

  function esc(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
})();
