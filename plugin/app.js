(function () {
  "use strict";

  var CTL_BIN = "/run/current-system/sw/bin/cockpit-nixos-ctl";
  var isAdmin = false;
  var busy = false;
  var permission = cockpit.permission({ admin: true });

  function $(id) {
    return document.getElementById(id);
  }

  function on(id, evt, handler) {
    var el = $(id);
    if (el) {
      el.addEventListener(evt, handler);
    }
  }

  /* Synchronisation exacte avec le thème Light/Dark/Auto de Cockpit (PatternFly 6) */
  function applyCockpitTheme() {
    var pref = "auto";
    try {
      pref = window.localStorage.getItem("shell:style") || "auto";
    } catch (e) {
      pref = "auto";
    }
    var prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    var isDark = (pref === "dark") || (pref === "auto" && prefersDark);
    document.documentElement.classList.toggle("pf-v6-theme-dark", isDark);
    document.documentElement.classList.toggle("pf-v5-theme-dark", isDark);
  }

  window.addEventListener("storage", function (e) {
    if (!e || e.key === "shell:style") {
      applyCockpitTheme();
    }
  });
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) {
      mq.addEventListener("change", applyCockpitTheme);
    }
  }
  applyCockpitTheme();

  function setLabel(id, text, colorModifier) {
    var el = $(id);
    if (!el) return;
    el.className = "pf-v6-c-label" + (colorModifier ? (" " + colorModifier) : "");
    var content = el.querySelector(".pf-v6-c-label__content");
    if (content) {
      content.textContent = text;
    } else {
      el.textContent = text;
    }
  }

  function makeLabel(text, colorModifier) {
    var span = document.createElement("span");
    span.className = "pf-v6-c-label" + (colorModifier ? (" " + colorModifier) : "");
    var inner = document.createElement("span");
    inner.className = "pf-v6-c-label__content";
    inner.textContent = text;
    span.appendChild(inner);
    return span;
  }

  function showAlert(msg, type) {
    var box = $("alert-banner");
    var title = $("alert-title");
    if (!box || !title) return;
    title.textContent = msg;
    box.className = "pf-v6-c-alert pf-m-inline " + (type === "error" ? "pf-m-danger" : "pf-m-success");
  }

  function hideAlert() {
    var box = $("alert-banner");
    if (box) {
      box.className = "pf-v6-c-alert pf-m-inline nixos-hidden";
    }
  }

  function setJobStatus(label, modifier) {
    setLabel("job-status", label, modifier);
  }

  function appendLog(chunk) {
    var out = $("console-output");
    if (!out) return;
    out.textContent += chunk;
    out.scrollTop = out.scrollHeight;
  }

  function resetLog(title) {
    var out = $("console-output");
    if (!out) return;
    out.textContent = ">>> " + title + " [" + new Date().toLocaleTimeString() + "]\n";
  }

  function setBusy(state) {
    busy = state;
    var buttons = document.querySelectorAll(
      "button[type='submit'], .admin-only, #btn-rebuild-now, #btn-dry-build, #btn-rollback, #btn-gc, #btn-optimise, #btn-sync-nm"
    );
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = state || !isAdmin;
    }
  }

  function updateAdminBadge() {
    if (permission.allowed) {
      isAdmin = true;
      setLabel("admin-badge", "Accès administrateur actif", "pf-m-green");
    } else {
      isAdmin = false;
      setLabel("admin-badge", "Lecture seule (activer l'accès admin en haut)", "pf-m-orange");
    }
    setBusy(busy);
  }

  permission.addEventListener("changed", updateAdminBadge);

  function spawnCtlRead(args) {
    return cockpit.spawn([CTL_BIN].concat(args), {
      superuser: "try",
      err: "message"
    });
  }

  function spawnCtlWrite(args, title) {
    if (!isAdmin) {
      showAlert("Activez d'abord l'accès administrateur dans la barre supérieure de Cockpit.", "error");
      return Promise.resolve(false);
    }
    hideAlert();
    setBusy(true);
    setJobStatus("En cours...", "pf-m-blue");
    resetLog(title);

    var proc = cockpit.spawn([CTL_BIN].concat(args), {
      superuser: "require",
      err: "out"
    });

    proc.stream(function (data) {
      appendLog(data);
    });

    return proc.then(function () {
      setJobStatus("Succès", "pf-m-green");
      showAlert(title + " : opération terminée avec succès.", "success");
      setBusy(false);
      return refreshAll().then(function () { return true; });
    }).catch(function (err) {
      appendLog("\n[ERREUR] " + (err.message || String(err)) + "\n");
      setJobStatus("Échec", "pf-m-red");
      showAlert(title + " : échec (" + (err.message || "voir le journal") + "). Rollback automatique appliqué si nécessaire.", "error");
      setBusy(false);
      return false;
    });
  }

  function renderGenerations(gens) {
    var tbody = $("generations-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!gens || gens.length === 0) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td colspan="5" class="pf-v6-c-table__td nixos-muted">Aucune génération trouvée.</td></tr>';
      return;
    }

    var activeGen = null;
    for (var i = 0; i < gens.length; i++) {
      var g = gens[i];
      if (g.current) {
        activeGen = g;
      }
      var tr = document.createElement("tr");
      tr.className = "pf-v6-c-table__tr";

      var tdId = document.createElement("td");
      tdId.className = "pf-v6-c-table__td";
      var strong = document.createElement("strong");
      strong.textContent = "#" + String(g.id);
      tdId.appendChild(strong);
      tr.appendChild(tdId);

      var tdDate = document.createElement("td");
      tdDate.className = "pf-v6-c-table__td";
      tdDate.textContent = g.date || "--";
      tr.appendChild(tdDate);

      var tdVer = document.createElement("td");
      tdVer.className = "pf-v6-c-table__td";
      tdVer.textContent = (g.nixos_version || "NixOS") + (g.kernel ? " (" + g.kernel + ")" : "");
      tr.appendChild(tdVer);

      var tdState = document.createElement("td");
      tdState.className = "pf-v6-c-table__td";
      tdState.appendChild(makeLabel(g.current ? "Active" : "Disponible", g.current ? "pf-m-green" : ""));
      tr.appendChild(tdState);

      var tdAct = document.createElement("td");
      tdAct.className = "pf-v6-c-table__td";
      if (!g.current) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pf-v6-c-button pf-m-secondary pf-m-small admin-only";
        btn.textContent = "Activer #" + String(g.id);
        btn.disabled = !isAdmin || busy;
        (function (genId) {
          btn.addEventListener("click", function () {
            if (window.confirm("Basculer le système sur la génération NixOS #" + genId + " ?")) {
              spawnCtlWrite(["switch-generation", String(genId)], "Activation génération #" + genId);
            }
          });
        })(g.id);
        tdAct.appendChild(btn);
      } else {
        var span = document.createElement("span");
        span.className = "nixos-muted";
        span.textContent = "Courante";
        tdAct.appendChild(span);
      }
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    }

    if ($("metric-gen-count")) {
      $("metric-gen-count").textContent = String(gens.length);
    }
    if (activeGen && $("metric-gen")) {
      $("metric-gen").textContent = "#" + String(activeGen.id);
      if ($("metric-gen-date")) {
        $("metric-gen-date").textContent = activeGen.date || "";
      }
    }
  }

  function renderPackages(pkgs) {
    var tbody = $("packages-tbody");
    if ($("metric-pkgs")) {
      $("metric-pkgs").textContent = String((pkgs || []).length);
    }
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!pkgs || pkgs.length === 0) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td colspan="3" class="pf-v6-c-table__td nixos-muted">Aucun paquet supplémentaire déclaré via Cockpit.</td></tr>';
      return;
    }

    for (var i = 0; i < pkgs.length; i++) {
      var name = pkgs[i];
      var tr = document.createElement("tr");
      tr.className = "pf-v6-c-table__tr";

      var tdName = document.createElement("td");
      tdName.className = "pf-v6-c-table__td";
      var code = document.createElement("code");
      code.textContent = name;
      tdName.appendChild(code);
      tr.appendChild(tdName);

      var tdSrc = document.createElement("td");
      tdSrc.className = "pf-v6-c-table__td";
      tdSrc.textContent = "/etc/nixos/cockpit-managed.nix";
      tr.appendChild(tdSrc);

      var tdAct = document.createElement("td");
      tdAct.className = "pf-v6-c-table__td";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pf-v6-c-button pf-m-danger pf-m-small admin-only";
      btn.textContent = "Supprimer";
      btn.disabled = !isAdmin || busy;
      (function (pkgName) {
        btn.addEventListener("click", function () {
          if (window.confirm("Retirer le paquet '" + pkgName + "' et lancer nixos-rebuild switch ?")) {
            spawnCtlWrite(["pkg-remove", pkgName], "Suppression paquet " + pkgName);
          }
        });
      })(name);
      tdAct.appendChild(btn);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    }
  }

  function renderNetworkAndFirewall(state) {
    var fw = (state && state.firewall) || {};
    var net = (state && state.network) || {};

    if ($("fw-enable")) $("fw-enable").checked = fw.enable !== false;
    if ($("fw-tcp")) $("fw-tcp").value = (fw.allowedTCPPorts || []).join(", ");
    if ($("fw-udp")) $("fw-udp").value = (fw.allowedUDPPorts || []).join(", ");
    if ($("fw-trusted")) $("fw-trusted").value = (fw.trustedInterfaces || []).join(", ");
    if ($("net-hostname")) $("net-hostname").value = net.hostname || "";
    if ($("net-dns")) $("net-dns").value = (net.nameservers || []).join(", ");

    var profiles = net.nmProfiles || {};
    var keys = Object.keys(profiles);
    if ($("metric-net-sub")) {
      $("metric-net-sub").textContent = String(keys.length);
    }

    var ul = $("nm-profiles-list");
    if (!ul) return;
    ul.innerHTML = "";
    if (keys.length === 0) {
      ul.innerHTML = '<li class="nixos-muted">Aucun profil NetworkManager intercepté pour le moment.</li>';
      return;
    }

    for (var i = 0; i < keys.length; i++) {
      var k = keys[i];
      var p = profiles[k];
      var li = document.createElement("li");
      var ipv4 = p.ipv4 || {};
      var conn = p.connection || {};
      var detail = (conn["interface-name"] || "auto") + " • " + (ipv4.method || "auto");
      if (ipv4.address1) {
        detail += " • " + ipv4.address1;
      }
      li.textContent = k + " (" + detail + ")";
      ul.appendChild(li);
    }
  }

  function renderMounts(mounts) {
    var tbody = $("mounts-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    var entries = mounts || [];
    if (entries.length === 0) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td colspan="5" class="pf-v6-c-table__td nixos-muted">Aucun montage déclaratif configuré via Cockpit.</td></tr>';
      return;
    }

    for (var i = 0; i < entries.length; i++) {
      var m = entries[i];
      var tr = document.createElement("tr");
      tr.className = "pf-v6-c-table__tr";

      var tdMp = document.createElement("td");
      tdMp.className = "pf-v6-c-table__td";
      var code = document.createElement("code");
      code.textContent = m.mountPoint;
      tdMp.appendChild(code);
      tr.appendChild(tdMp);

      var tdDev = document.createElement("td");
      tdDev.className = "pf-v6-c-table__td";
      tdDev.textContent = m.device;
      tr.appendChild(tdDev);

      var tdFs = document.createElement("td");
      tdFs.className = "pf-v6-c-table__td";
      tdFs.textContent = m.fsType;
      tr.appendChild(tdFs);

      var tdOpt = document.createElement("td");
      tdOpt.className = "pf-v6-c-table__td";
      tdOpt.textContent = (m.options || []).join(",");
      tr.appendChild(tdOpt);

      var tdAct = document.createElement("td");
      tdAct.className = "pf-v6-c-table__td";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "pf-v6-c-button pf-m-danger pf-m-small admin-only";
      btn.textContent = "Retirer";
      btn.disabled = !isAdmin || busy;
      (function (mp) {
        btn.addEventListener("click", function () {
          if (window.confirm("Retirer le montage déclaratif '" + mp + "' ?")) {
            spawnCtlWrite(["mount-remove", mp], "Suppression montage " + mp);
          }
        });
      })(m.mountPoint);
      tdAct.appendChild(btn);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    }
  }

  function renderTelemetry(hw) {
    var fanEl = $("metric-fan");
    var tempEl = $("metric-temp");
    var grid = $("hwmon-details");
    if (!fanEl && !grid) return;

    if (!hw) {
      if (fanEl) fanEl.textContent = "N/A";
      if (tempEl) tempEl.textContent = "Aucun capteur";
      return;
    }

    if (fanEl) {
      fanEl.textContent = (hw.fan_rpm !== null && hw.fan_rpm !== undefined) ? (String(hw.fan_rpm) + " RPM") : "Auto / EC";
    }
    if (tempEl) {
      if (hw.apu_temp_c !== null && hw.apu_temp_c !== undefined) {
        tempEl.textContent = "APU/CPU : " + hw.apu_temp_c.toFixed(1) + " °C" + (hw.steamdeck_detected ? " (Steam Deck)" : "");
      } else {
        tempEl.textContent = hw.steamdeck_detected ? "Driver steamdeck_hwmon détecté" : "Capteurs standards";
      }
    }

    if (!grid) return;
    grid.innerHTML = "";
    var sensors = hw.sensors || [];
    if (sensors.length === 0) {
      grid.innerHTML = '<div class="nixos-muted">Aucun périphérique hwmon lisible.</div>';
      return;
    }
    for (var i = 0; i < sensors.length; i++) {
      var s = sensors[i];
      var div = document.createElement("div");
      div.className = "nixos-probe-item";
      var strong = document.createElement("strong");
      strong.textContent = s.chip + " — " + s.label;
      var val = document.createElement("div");
      val.textContent = s.value;
      div.appendChild(strong);
      div.appendChild(val);
      grid.appendChild(div);
    }
  }

  function refreshAll() {
    return spawnCtlRead(["status"]).then(function (raw) {
      var data = JSON.parse(raw);
      renderGenerations(data.generations);
      renderPackages(data.state.packages);
      renderNetworkAndFirewall(data.state);
      renderMounts(data.state.mounts);
      renderTelemetry(data.telemetry);
      if ($("nix-preview")) {
        $("nix-preview").textContent = data.managed_nix || "# Fichier non généré";
      }
    }).catch(function (err) {
      showAlert("Impossible de lire l'état NixOS : " + (err.message || err), "error");
    });
  }

  function setupEvents() {
    on("btn-refresh-all", "click", refreshAll);
    on("btn-reload-nix", "click", refreshAll);
    on("btn-clear-log", "click", function () {
      if ($("console-output")) $("console-output").textContent = "";
    });

    /* Navigation directe vers les modules natifs Cockpit */
    on("btn-open-network", "click", function () { cockpit.jump("/network"); });
    on("btn-open-firewall", "click", function () { cockpit.jump("/network/firewall"); });
    on("btn-open-files", "click", function () { cockpit.jump("/files"); });
    on("btn-open-storage", "click", function () { cockpit.jump("/storage"); });
    on("btn-open-updates", "click", function () { cockpit.jump("/updates"); });

    on("btn-rebuild-now", "click", function () {
      spawnCtlWrite(["rebuild"], "nixos-rebuild switch");
    });

    on("btn-dry-build", "click", function () {
      spawnCtlWrite(["dry-build"], "nixos-rebuild dry-build");
    });

    on("btn-rollback", "click", function () {
      if (window.confirm("Revenir immédiatement à la génération NixOS précédente (nixos-rebuild switch --rollback) ?")) {
        spawnCtlWrite(["rollback"], "Rollback NixOS");
      }
    });

    on("btn-gc", "click", function () {
      if (window.confirm("Supprimer TOUTES les anciennes générations NixOS et exécuter nix-collect-garbage -d ? Cette action est irréversible.")) {
        spawnCtlWrite(["gc"], "Garbage Collection (nix-collect-garbage -d)");
      }
    });

    on("btn-optimise", "click", function () {
      spawnCtlWrite(["optimise-store"], "Optimisation Nix Store (déduplication hardlinks)");
    });

    on("btn-sync-nm", "click", function () {
      spawnCtlWrite(["sync-networkmanager"], "Import déclaratif des profils NetworkManager");
    });

    on("form-add-pkg", "submit", function (e) {
      e.preventDefault();
      var pkg = $("input-pkg-name").value.trim();
      if (!pkg) return;
      spawnCtlWrite(["pkg-add", pkg], "Installation déclarative de " + pkg).then(function (ok) {
        if (ok) {
          $("input-pkg-name").value = "";
        }
      });
    });

    on("btn-search-pkg", "click", function () {
      var q = $("input-pkg-name").value.trim();
      var box = $("pkg-search-results");
      if (q.length < 2) {
        showAlert("Entrez au moins 2 caractères pour rechercher dans nixpkgs.", "error");
        return;
      }
      box.classList.remove("nixos-hidden");
      box.textContent = "Recherche de '" + q + "' dans nixpkgs...";
      spawnCtlRead(["pkg-search", q]).then(function (raw) {
        var results = JSON.parse(raw);
        box.innerHTML = "";
        if (!results || results.length === 0) {
          box.textContent = "Aucun résultat trouvé pour '" + q + "'.";
          return;
        }
        for (var i = 0; i < results.length; i++) {
          var item = results[i];
          var row = document.createElement("div");
          row.className = "nixos-search-row";
          var info = document.createElement("span");
          var code = document.createElement("code");
          code.textContent = item.attr;
          info.appendChild(code);
          info.appendChild(document.createTextNode(" — " + item.pname + (item.description ? (" (" + item.description + ")") : "")));
          var addBtn = document.createElement("button");
          addBtn.type = "button";
          addBtn.className = "pf-v6-c-button pf-m-primary pf-m-small admin-only";
          addBtn.textContent = "Installer";
          addBtn.disabled = !isAdmin || busy;
          (function (attr) {
            addBtn.addEventListener("click", function () {
              spawnCtlWrite(["pkg-add", attr], "Installation déclarative de " + attr);
            });
          })(item.attr);
          row.appendChild(info);
          row.appendChild(addBtn);
          box.appendChild(row);
        }
      }).catch(function (err) {
        box.textContent = "Erreur de recherche : " + (err.message || err);
      });
    });

    on("form-firewall", "submit", function (e) {
      e.preventDefault();
      var payload = JSON.stringify({
        enable: $("fw-enable").checked,
        tcp: $("fw-tcp").value,
        udp: $("fw-udp").value,
        trusted: $("fw-trusted").value
      });
      spawnCtlWrite(["fw-set", payload], "Mise à jour déclarative du pare-feu");
    });

    on("form-network", "submit", function (e) {
      e.preventDefault();
      var payload = JSON.stringify({
        hostname: $("net-hostname").value.trim(),
        nameservers: $("net-dns").value
      });
      spawnCtlWrite(["net-set", payload], "Mise à jour déclarative du réseau");
    });

    on("form-add-mount", "submit", function (e) {
      e.preventDefault();
      var payload = JSON.stringify({
        mountPoint: $("mnt-point").value.trim(),
        device: $("mnt-device").value.trim(),
        fsType: $("mnt-fstype").value,
        options: $("mnt-options").value.trim()
      });
      spawnCtlWrite(["mount-add", payload], "Ajout montage déclaratif " + $("mnt-point").value.trim()).then(function (ok) {
        if (ok) {
          $("mnt-point").value = "";
          $("mnt-device").value = "";
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyCockpitTheme();
    setupEvents();
    updateAdminBadge();
    refreshAll();
    if (document.body.getAttribute("data-page") === "generations") {
      window.setInterval(function () {
        if (!busy) {
          spawnCtlRead(["telemetry"]).then(function (raw) {
            renderTelemetry(JSON.parse(raw));
          }).catch(function () {});
        }
      }, 10000);
    }
  });
})();
