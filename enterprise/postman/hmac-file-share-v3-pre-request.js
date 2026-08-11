/*
 * FILE_SHARE v3 HMAC pre-request signer.
 *
 * The canonical input deliberately mirrors
 * enterprise.gateway.auth.service_auth.canonical_request/sign_request:
 * v1, timestamp, uppercase method, RFC3986 path + sorted query, and the
 * SHA-256 digest of the exact raw UTF-8 request body, one field per line.
 */
(async function signFileShareRequest() {
    function rfc3986Encode(value) {
        return encodeURIComponent(String(value)).replace(/[!'()*]/g, function (character) {
            return "%" + character.charCodeAt(0).toString(16).toUpperCase();
        });
    }

    function decodeQueryComponent(value) {
        return decodeURIComponent(String(value).replace(/\+/g, " "));
    }

    function canonicalPathQuery(path, query) {
        var normalizedPath = rfc3986Encode(decodeURIComponent(path || "/"))
            .replace(/%2F/gi, "/");
        var rawQuery = String(query || "");
        var pairs = [];
        if (rawQuery !== "") {
            rawQuery.split("&").forEach(function (part) {
                if (part === "") return;
                var separator = part.indexOf("=");
                var rawKey = separator < 0 ? part : part.slice(0, separator);
                var rawValue = separator < 0 ? "" : part.slice(separator + 1);
                pairs.push({
                    key: rfc3986Encode(decodeQueryComponent(rawKey)),
                    value: rfc3986Encode(decodeQueryComponent(rawValue))
                });
            });
        }
        pairs.sort(function (left, right) {
            if (left.key !== right.key) return left.key < right.key ? -1 : 1;
            if (left.value === right.value) return 0;
            return left.value < right.value ? -1 : 1;
        });
        if (!pairs.length) return normalizedPath;
        return normalizedPath + "?" + pairs.map(function (pair) {
            return pair.key + "=" + pair.value;
        }).join("&");
    }

    function readVariable(name) {
        return String(pm.variables.get(name) || "");
    }

    async function readSecret(name) {
        var value = "";
        if (pm.vault && typeof pm.vault.get === "function") {
            try {
                value = await pm.vault.get(name);
            } catch (error) {
                value = "";
            }
        }
        return String(value || readVariable(name));
    }

    var keyId = readVariable("keyId");
    var secret = await readSecret("hmacSecret");
    if (!keyId || !secret) throw new Error("keyId and hmacSecret are required");

    var configuredTimestamp = readVariable("hmacTimestamp");
    var timestamp = /^\d{10}$/.test(configuredTimestamp)
        ? configuredTimestamp
        : String(Math.floor(Date.now() / 1000));
    if (!/^\d{10}$/.test(timestamp)) throw new Error("timestamp must be a ten-digit epoch value");

    var path = pm.request.url.getPath();
    var query = pm.request.url.getQueryString();
    var rawBody = pm.request.body && pm.request.body.mode === "raw"
        ? pm.variables.replaceIn(pm.request.body.raw || "")
        : "";
    var target = canonicalPathQuery(path, query);
    var bodyHash = CryptoJS.SHA256(CryptoJS.enc.Utf8.parse(rawBody)).toString(CryptoJS.enc.Hex);
    var canonical = ["v1", timestamp, pm.request.method.toUpperCase(), target, bodyHash].join("\n");
    var digest = CryptoJS.HmacSHA256(canonical, secret).toString(CryptoJS.enc.Hex);

    pm.request.headers.upsert({ key: "X-TY-Timestamp", value: timestamp });
    pm.request.headers.upsert({ key: "X-TY-Key-Id", value: keyId });
    pm.request.headers.upsert({ key: "X-TY-Signature", value: "v1=" + digest });
})();
