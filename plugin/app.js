(function () {
  "use strict";

  var CTL_BIN = "/run/current-system/sw/bin/cockpit-nixos-ctl";
  var isAdmin = false;
  var busy = false;
  var lastPublishedStatus = null;
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

  /* Notification native vers la page Aperçu (Overview) de Cockpit */
  function publishPageStatus(statusObj) {
    if (document.body.getAttribute("data-page") !== "updates") return;
    var serialized = JSON.stringify(statusObj);
    if (serialized === lastPublishedStatus) return;
    lastPublishedStatus = serialized;
    try {
      if (cockpit && cockpit.transport && typeof cockpit.transport.control === "function") {
        cockpit.transport.control("notify", { page_status: statusObj });
      }
    } catch (e) {}
  }

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
      "button[type='submit'], .admin-only, #btn-rebuild-now, #btn-dry-build, #btn-rollback, #btn-gc, #btn-optimise, #btn-check-updates, #btn-apply-updates, #btn-reboot"
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

    /* Passe par sudo -n (règle NOPASSWD dédiée à cockpit-nixos-ctl) plutôt que le
       bridge Polkit (superuser: "require"), car nixos-rebuild switch redémarre
       polkit.service et recharge dbus-broker.service lors de l'activation ! */
    var proc = cockpit.spawn(["/run/wrappers/bin/sudo", "-n", CTL_BIN].concat(args), {
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

  function renderUpdatesAndStatus(data) {
    if (document.body.getAttribute("data-page") !== "updates") return;

    var updatesObj = data.updates || {};
    var updates = updatesObj.updates || [];
    var ts = Number(updatesObj.timestamp || 0);
    var sys = data.system_info || {};
    var gens = data.generations || [];
    var activeGen = null;
    for (var i = 0; i < gens.length; i++) {
      if (gens[i].current) {
        activeGen = gens[i];
        break;
      }
    }

    var hasSecurity = false;
    for (var j = 0; j < updates.length; j++) {
      if (updates[j].security || updates[j].info === 8) {
        hasSecurity = true;
        break;
      }
    }

    var rebootBtn = $("btn-reboot");
    if (rebootBtn) {
      rebootBtn.classList.toggle("nixos-hidden", !sys.reboot_required);
    }

    if (sys.reboot_required) {
      setLabel("updates-status-badge", "Redémarrage recommandé (nouveau noyau)", "pf-m-orange");
      publishPageStatus({
        type: "warning",
        title: "Redémarrage recommandé pour activer le nouveau noyau NixOS",
        details: { pficon: "reboot" }
      });
    } else if (updates.length > 0) {
      var lbl = String(updates.length) + " mise(s) à jour disponible(s)";
      setLabel("updates-status-badge", lbl, hasSecurity ? "pf-m-orange" : "pf-m-blue");
      publishPageStatus({
        type: hasSecurity ? "warning" : "info",
        title: lbl,
        details: { pficon: hasSecurity ? "security" : "enhancement" }
      });
    } else {
      setLabel("updates-status-badge", "Le système NixOS est à jour", "pf-m-green");
      publishPageStatus({
        type: null,
        title: "Le système NixOS est à jour",
        details: { pficon: "check" }
      });
    }

    if ($("updates-status-detail")) {
      $("updates-status-detail").textContent = activeGen
        ? ("Génération active : #" + String(activeGen.id) + " (" + (activeGen.date || "--") + ")")
        : "Génération active : --";
    }
    if ($("updates-kernel-detail")) {
      var kTxt = sys.booted_kernel || (activeGen && activeGen.kernel) || "--";
      if (sys.reboot_required && sys.next_kernel && sys.next_kernel !== sys.booted_kernel) {
        kTxt += " -> prochain boot : " + sys.next_kernel;
      }
      $("updates-kernel-detail").textContent = "Noyau : " + kTxt;
    }

    if ($("updates-last-check")) {
      $("updates-last-check").textContent = ts > 0
        ? ("Dernière vérification : " + new Date(ts * 1000).toLocaleString())
        : "Dernière vérification : jamais (cliquez sur Vérifier les mises à jour)";
    }

    var listCard = $("updates-list-card");
    var tbody = $("updates-tbody");
    if ($("updates-count")) {
      $("updates-count").textContent = String(updates.length);
    }
    if (listCard && tbody) {
      tbody.innerHTML = "";
      if (updates.length === 0) {
        listCard.classList.add("nixos-hidden");
      } else {
        listCard.classList.remove("nixos-hidden");
        for (var uIdx = 0; uIdx < updates.length; uIdx++) {
          var u = updates[uIdx];
          var tr = document.createElement("tr");
          tr.className = "pf-v6-c-table__tr";

          var tdName = document.createElement("td");
          tdName.className = "pf-v6-c-table__td";
          var code = document.createElement("code");
          code.textContent = u.name || (u.id || "").split(";")[0];
          tdName.appendChild(code);
          tr.appendChild(tdName);

          var tdVer = document.createElement("td");
          tdVer.className = "pf-v6-c-table__td";
          tdVer.textContent = (u.old_version || "--") + " -> " + (u.new_version || "--");
          tr.appendChild(tdVer);

          var tdSev = document.createElement("td");
          tdSev.className = "pf-v6-c-table__td";
          var isSec = Boolean(u.security || u.info === 8);
          tdSev.appendChild(makeLabel(isSec ? "Sécurité" : "Mise à jour", isSec ? "pf-m-orange" : "pf-m-blue"));
          tr.appendChild(tdSev);

          tbody.appendChild(tr);
        }
      }
    }
  }

  function renderGenerations(gens) {
    var tbody = $("generations-tbody");
    if ($("metric-gen-count")) {
      $("metric-gen-count").textContent = String((gens || []).length);
    }
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!gens || gens.length === 0) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td colspan="5" class="pf-v6-c-table__td nixos-muted">Aucune génération trouvée.</td></tr>';
      return;
    }

    for (var i = 0; i < gens.length; i++) {
      var g = gens[i];
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
      var badges = document.createElement("div");
      badges.className = "nixos-actions";
      if (g.current) {
        badges.appendChild(makeLabel("Active", "pf-m-green"));
      }
      if (g.booted && !g.current) {
        badges.appendChild(makeLabel("Démarrée", "pf-m-blue"));
      }
      if (!g.current && !g.booted) {
        badges.appendChild(makeLabel("Disponible", ""));
      }
      tdState.appendChild(badges);
      tr.appendChild(tdState);

      var tdAct = document.createElement("td");
      tdAct.className = "pf-v6-c-table__td";
      if (!g.current) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pf-v6-c-button pf-m-secondary pf-m-small admin-only";
        btn.textContent = "Activer / Rollback";
        btn.disabled = !isAdmin || busy;
        (function (genId) {
          btn.addEventListener("click", function () {
            if (window.confirm("Basculer immédiatement le système sur la génération NixOS #" + genId + " ?")) {
              spawnCtlWrite(["switch-generation", String(genId)], "Rollback vers génération #" + genId);
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

  function renderTelemetry(hw) {
    var card = $("hwmon-card");
    var tempEl = $("metric-temp");
    var grid = $("hwmon-details");
    if (!grid) return;

    var sensors = (hw && hw.sensors) || [];
    if (card) {
      card.classList.toggle("nixos-hidden", sensors.length === 0);
    }
    if (sensors.length === 0) return;

    if (tempEl) {
      var parts = [];
      if (hw.fan_rpm !== null && hw.fan_rpm !== undefined) {
        parts.push("Ventilateur : " + String(hw.fan_rpm) + " RPM");
      }
      if (hw.apu_temp_c !== null && hw.apu_temp_c !== undefined) {
        parts.push("APU/CPU : " + hw.apu_temp_c.toFixed(1) + " °C");
      }
      if (hw.steamdeck_detected) {
        parts.push("Steam Deck");
      }
      tempEl.textContent = parts.join(" • ");
    }

    grid.innerHTML = "";
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
      renderUpdatesAndStatus(data);
      renderGenerations(data.generations);
      renderPackages(data.state.packages);
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

    on("btn-open-updates", "click", function () {
      cockpit.jump("/updates");
    });

    on("btn-check-updates", "click", function () {
      spawnCtlWrite(["updates-check"], "Vérification des mises à jour NixOS");
    });

    on("btn-apply-updates", "click", function () {
      if (window.confirm("Installer les mises à jour NixOS (nixos-rebuild switch --upgrade) et créer une nouvelle génération ?")) {
        spawnCtlWrite(["updates-apply"], "Installation des mises à jour NixOS");
      }
    });

    on("btn-reboot", "click", function () {
      if (window.confirm("Redémarrer le système maintenant pour activer le nouveau noyau ?")) {
        spawnCtlWrite(["reboot"], "Redémarrage du système");
      }
    });

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
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyCockpitTheme();
    setupEvents();
    updateAdminBadge();
    refreshAll();
    if (document.body.getAttribute("data-page") === "updates") {
      window.setInterval(function () {
        if (!busy) {
          spawnCtlRead(["telemetry"]).then(function (raw) {
            renderTelemetry(JSON.parse(raw));
          }).catch(function () {});
        }
      }, 15000);
    }
  });
})();
