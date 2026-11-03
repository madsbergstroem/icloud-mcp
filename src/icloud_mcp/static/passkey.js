// Passkey sign-in and registration for the icloud-mcp dashboard app (window.Passkey). Served from the server itself
// (CSP script-src 'self').
(function () {
  "use strict";
  var b64 = {
    toBuf: function (s) {
      s = s.replace(/-/g, "+").replace(/_/g, "/");
      while (s.length % 4) s += "=";
      var bin = atob(s), out = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out.buffer;
    },
    fromBuf: function (buf) {
      var bytes = new Uint8Array(buf), bin = "";
      for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }
  };

  function requestOptions(o) {
    if (PublicKeyCredential.parseRequestOptionsFromJSON) return PublicKeyCredential.parseRequestOptionsFromJSON(o);
    o.challenge = b64.toBuf(o.challenge);
    (o.allowCredentials || []).forEach(function (c) { c.id = b64.toBuf(c.id); });
    return o;
  }

  function creationOptions(o) {
    if (PublicKeyCredential.parseCreationOptionsFromJSON) return PublicKeyCredential.parseCreationOptionsFromJSON(o);
    o.challenge = b64.toBuf(o.challenge);
    o.user.id = b64.toBuf(o.user.id);
    (o.excludeCredentials || []).forEach(function (c) { c.id = b64.toBuf(c.id); });
    return o;
  }

  function credentialJSON(c) {
    if (typeof c.toJSON === "function") return c.toJSON();
    var r = c.response, out = { id: c.id, rawId: b64.fromBuf(c.rawId), type: c.type, clientExtensionResults: c.getClientExtensionResults(),
      authenticatorAttachment: c.authenticatorAttachment || null, response: { clientDataJSON: b64.fromBuf(r.clientDataJSON) } };
    if (r.attestationObject) {
      out.response.attestationObject = b64.fromBuf(r.attestationObject);
      if (r.getTransports) out.response.transports = r.getTransports();
    } else {
      out.response.authenticatorData = b64.fromBuf(r.authenticatorData);
      out.response.signature = b64.fromBuf(r.signature);
      if (r.userHandle) out.response.userHandle = b64.fromBuf(r.userHandle);
    }
    return out;
  }

  function post(url, body, csrf) {
    var headers = { "Content-Type": "application/json" };
    if (csrf) headers["X-CSRF"] = csrf;
    return fetch(url, { method: "POST", headers: headers, credentials: "same-origin", body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { j._status = r.status; return j; }); });
  }

  // For the app (static/app): the same steps as promises. signin() also confirms an existing session; register() needs it recent.
  function ceremony(purpose, csrf, extra) {
    if (!window.PublicKeyCredential) return Promise.reject(new Error("This browser cannot use passkeys here. Use the owner password."));
    return post("/dashboard/passkey/options", { purpose: purpose }, csrf).then(function (opts) {
      if (opts.error === "confirm") { var e = new Error("confirm"); e.confirm = true; throw e; }
      if (opts.error) throw new Error(opts.error);
      var call = purpose === "register"
        ? navigator.credentials.create({ publicKey: creationOptions(opts.publicKey) })
        : navigator.credentials.get({ publicKey: requestOptions(opts.publicKey) });
      return call.then(function (cred) {
        return post("/dashboard/passkey/verify", Object.assign({ purpose: purpose, ticket: opts.ticket, credential: credentialJSON(cred) }, extra || {}), csrf);
      });
    }).then(function (res) {
      if (res.error) throw new Error(res.error);
      return res;
    }, function (e) {
      if (e && e.name === "NotAllowedError") throw new Error("The passkey prompt was closed or timed out. Try again.");
      throw e;
    });
  }
  window.Passkey = {
    available: function () { return !!window.PublicKeyCredential; },
    signin: function (csrf) { return ceremony("signin", csrf || "", { next: "/dashboard" }); },
    register: function (name, csrf) { return ceremony("register", csrf, { name: name || "" }); }
  };

})();
