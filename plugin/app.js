(function () {
  "use strict";

  var CTL_BIN = "/run/current-system/sw/bin/cockpit-nixos-ctl";
  var isAdmin = false;
  var busy = false;
  var currentState = null;

  var permission = cockpit.permission({ admin: true });

  function $(id) {
    return document.getElementById(id);
  }

  function showAlert(msg, type) {
    var el = $("alert-banner");
    el.textContent = msg;
    el.className = "alert " + (type === "error" ? "alert-error" : "alert-success");
  }

  function hideAlert() {
    $("alert-banner").className = "alert hidden";
  }

  function setJobStatus(label, kind) {
    var el = $("job-status");
    el.textContent = label;
    el.className = "badge badge-" + kind;
  }

  function appendLog(chunk) {
    var out = $("console-output");
    out.textContent += chunk;
    out.scrollTop = out.scrollHeight;
  }

  function resetLog(title) {
    var out = $("console-output");
    out.textContent = ">>> " + title + " [" + new Date().toLocaleTimeString() + "]\n";
  }

  function setBusy(state) {
    busy = state;
    var buttons = document.querySelectorAll("button[type='submit'], #btn-rebuild-now, #btn-dry-build, #btn-rollback, #btn-gc, #btn-optimise, #btn-sync-nm, #btn-health-check, #btn-trigger-upgrade");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].disabled = state || !isAdmin;
    }
  }

  function updateAdminBadge() {
    var badge = $("admin-badge");
    if (permission.allowed) {
      isAdmin = true;
      badge.textContent = "Accès administrateur actif";
      badge.className = "badge badge-ok";
    } else {
      isAdmin = false;
      badge.textContent = "Lecture seule (activer l'accès admin en haut)";
      badge.className = "badge badge-warn";
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
      return Promise.reject(new Error("admin required"));
    }
    hideAlert();
    setBusy(true);
    setJobStatus("En cours...", "running");
    resetLog(title);

    var proc = cockpit.spawn(["sudo", "-n", CTL_BIN].concat(args), {
      superuser: "try",
      err: "out"
    });

    proc.stream(function (data) {
      appendLog(data);
    });

    return proc.then(function () {
      setJobStatus("Succès", "ok");
      showAlert(title + " : opération terminée avec succès.", "success");
      setBusy(false);
      return refreshAll();
    }).catch(function (err) {
      appendLog("\n[ERREUR] " + (err.message || String(err)) + "\n");
      setJobStatus("Échec", "err");
      showAlert(title + " : échec (" + (err.message || "voir le journal") + "). Rollback automatique appliqué si nécessaire.", "error");
      setBusy(false);
      throw err;
    });
  }

  function renderGenerations(gens) {
    var tbody = $("generations-tbody");
    tbody.innerHTML = "";
    if (!gens || gens.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">Aucune génération trouvée.</td></tr>';
      return;
    }

    var activeGen = null;
    for (var i = 0; i < gens.length; i++) {
      var g = gens[i];
      if (g.current) {
        activeGen = g;
      }
      var tr = document.createElement("tr");

      var tdId = document.createElement("td");
      tdId.innerHTML = "<strong>#" + String(g.id) + "</strong>";
      tr.appendChild(tdId);

      var tdDate = document.createElement("td");
      tdDate.textContent = g.date || "--";
      tr.appendChild(tdDate);

      var tdVer = document.createElement("td");
      tdVer.textContent = (g.nixos_version || "NixOS") + (g.kernel ? " (" + g.kernel + ")" : "");
      tr.appendChild(tdVer);

      var tdState = document.createElement("td");
      var badge = document.createElement("span");
      badge.className = g.current ? "badge badge-ok" : "badge badge-idle";
      badge.textContent = g.current ? "Active" : "Disponible";
      tdState.appendChild(badge);
      tr.appendChild(tdState);

      var tdAct = document.createElement("td");
      if (!g.current) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-small btn-secondary";
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
        tdAct.innerHTML = '<span class="muted">Courante</span>';
      }
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    }

    if (activeGen) {
      $("metric-gen").textContent = "#" + String(activeGen.id);
      $("metric-gen-date").textContent = activeGen.date || "";
    }
  }

  function renderPackages(pkgs) {
    var tbody = $("packages-tbody");
    tbody.innerHTML = "";
    $("metric-pkgs").textContent = String((pkgs || []).length);

    if (!pkgs || pkgs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="3" class="muted">Aucun paquet supplémentaire déclaré via Cockpit.</td></tr>';
      return;
    }

    for (var i = 0; i < pkgs.length; i++) {
      var name = pkgs[i];
      var tr = document.createElement("tr");

      var tdName = document.createElement("td");
      var code = document.createElement("code");
      code.textContent = name;
      tdName.appendChild(code);
      tr.appendChild(tdName);

      var tdSrc = document.createElement("td");
      tdSrc.textContent = "cockpit-managed.nix";
      tr.appendChild(tdSrc);

      var tdAct = document.createElement("td");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-small btn-danger";
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

    $("fw-enable").checked = fw.enable !== false;
    $("fw-tcp").value = (fw.allowedTCPPorts || []).join(", ");
    $("fw-udp").value = (fw.allowedUDPPorts || []).join(", ");
    $("fw-trusted").value = (fw.trustedInterfaces || []).join(", ");

    $("net-hostname").value = net.hostname || "";
    $("net-dns").value = (net.nameservers || []).join(", ");

    var tcpCount = (fw.allowedTCPPorts || []).length;
    var udpCount = (fw.allowedUDPPorts || []).length;
    $("metric-fw").textContent = (fw.enable === false ? "Désactivé" : "Actif") + " (" + tcpCount + " TCP / " + udpCount + " UDP)";

    var profiles = net.nmProfiles || {};
    var keys = Object.keys(profiles);
    $("metric-net-sub").textContent = keys.length + " profil(s) NM déclaratif(s)";

    var ul = $("nm-profiles-list");
    ul.innerHTML = "";
    if (keys.length === 0) {
      ul.innerHTML = '<li class="muted">Aucun profil NetworkManager intercepté pour le moment.</li>';
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
    tbody.innerHTML = "";
    var entries = mounts || [];
    if (entries.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">Aucun montage déclaratif configuré via Cockpit.</td></tr>';
      return;
    }

    for (var i = 0; i < entries.length; i++) {
      var m = entries[i];
      var tr = document.createElement("tr");

      var tdMp = document.createElement("td");
      tdMp.innerHTML = "<code></code>";
      tdMp.firstChild.textContent = m.mountPoint;
      tr.appendChild(tdMp);

      var tdDev = document.createElement("td");
      tdDev.textContent = m.device;
      tr.appendChild(tdDev);

      var tdFs = document.createElement("td");
      tdFs.textContent = m.fsType;
      tr.appendChild(tdFs);

      var tdOpt = document.createElement("td");
      tdOpt.textContent = (m.options || []).join(",");
      tr.appendChild(tdOpt);

      var tdAct = document.createElement("td");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-small btn-danger";
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
    grid.innerHTML = "";

    if (!hw) {
      fanEl.textContent = "N/A";
      tempEl.textContent = "Aucun capteur";
      return;
    }

    if (hw.fan_rpm !== null && hw.fan_rpm !== undefined) {
      fanEl.textContent = String(hw.fan_rpm) + " RPM";
    } else {
      fanEl.textContent = "Auto / EC";
    }

    if (hw.apu_temp_c !== null && hw.apu_temp_c !== undefined) {
      tempEl.textContent = "APU/CPU : " + hw.apu_temp_c.toFixed(1) + " °C" + (hw.steamdeck_detected ? " (Steam Deck)" : "");
    } else {
      tempEl.textContent = hw.steamdeck_detected ? "Driver steamdeck_hwmon détecté" : "Capteurs standards";
    }

    var sensors = hw.sensors || [];
    if (sensors.length === 0) {
      grid.innerHTML = '<div class="muted">Aucun périphérique hwmon lisible.</div>';
      return;
    }
    for (var i = 0; i < sensors.length; i++) {
      var s = sensors[i];
      var div = document.createElement("div");
      div.className = "hwmon-item";
      var strong = document.createElement("strong");
      strong.textContent = s.chip + " — " + s.label;
      var val = document.createElement("div");
      val.textContent = s.value;
      div.appendChild(strong);
      div.appendChild(val);
      grid.appendChild(div);
    }
  }

  function renderHealth(hc) {
    var valEl = $("metric-health");
    var subEl = $("metric-health-sub");
    var infoEl = $("health-upgrade-info");
    var grid = $("health-probes-grid");
    var rbList = $("health-rollbacks-list");

    if (!hc) {
      valEl.textContent = "Inactif";
      subEl.textContent = "Health-checker non configuré";
      return;
    }

    var st = hc.status || "UNKNOWN";
    valEl.textContent = st === "HEALTHY" ? "Sain (Bénie)" : st;
    var lg = (hc.last_good_gen !== null && hc.last_good_gen !== undefined) ? ("#" + String(hc.last_good_gen)) : "--";
    subEl.textContent = "Last Good: " + lg + " • Essais: " + String(hc.unhealthy_boot_attempts || 0) + "/" + String(hc.max_boot_attempts || 3);

    var upgTxt = hc.auto_upgrade_active ? ("Auto-MAJ active (prochaine : " + (hc.auto_upgrade_next || "planifiée") + ")") : "Auto-MAJ inactive";
    infoEl.textContent = upgTxt + (hc.last_check_time ? (" — Dernier check : " + hc.last_check_time) : "");

    grid.innerHTML = "";
    var probes = hc.probes || [];
    if (probes.length === 0) {
      grid.innerHTML = '<div class="muted">En attente du premier health-check (cliquez sur Tester les sondes).</div>';
    } else {
      for (var i = 0; i < probes.length; i++) {
        var p = probes[i];
        var item = document.createElement("div");
        item.className = "hwmon-item";
        var title = document.createElement("strong");
        title.textContent = (p.ok ? "[OK] " : "[ÉCHEC] ") + p.name;
        var detail = document.createElement("div");
        detail.textContent = p.detail || "";
        item.appendChild(title);
        item.appendChild(detail);
        grid.appendChild(item);
      }
    }

    rbList.innerHTML = "";
    var hist = hc.rollback_history || [];
    if (hist.length === 0) {
      rbList.innerHTML = '<li class="muted">Aucun rollback automatique déclenché.</li>';
    } else {
      for (var j = hist.length - 1; j >= 0; j--) {
        var ev = hist[j];
        var li = document.createElement("li");
        li.textContent = ev.time + " : #" + String(ev.from_gen) + " -> #" + String(ev.to_gen) + " (" + (ev.reason || "") + ")";
        rbList.appendChild(li);
      }
    }
  }

  function refreshAll() {
    var pState = spawnCtlRead(["status"]).then(function (raw) {
      var data = JSON.parse(raw);
      currentState = data.state;
      renderGenerations(data.generations);
      renderPackages(data.state.packages);
      renderNetworkAndFirewall(data.state);
      renderMounts(data.state.mounts);
      renderTelemetry(data.telemetry);
      renderHealth(data.health);
      $("nix-preview").textContent = data.managed_nix || "# Fichier non généré";
    }).catch(function (err) {
      showAlert("Impossible de lire l'état NixOS : " + (err.message || err), "error");
    });
    return pState;
  }

  function setupTabs() {
    var tabs = document.querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener("click", function () {
        var target = this.getAttribute("data-tab");
        var allTabs = document.querySelectorAll(".tab");
        var allPanels = document.querySelectorAll(".panel");
        for (var j = 0; j < allTabs.length; j++) {
          allTabs[j].classList.remove("active");
        }
        for (var k = 0; k < allPanels.length; k++) {
          allPanels[k].classList.remove("active");
        }
        this.classList.add("active");
        $("panel-" + target).classList.add("active");
      });
    }
  }

  function setupEvents() {
    $("btn-refresh-all").addEventListener("click", refreshAll);
    $("btn-reload-nix").addEventListener("click", refreshAll);
    $("btn-clear-log").addEventListener("click", function () {
      $("console-output").textContent = "";
    });

    $("btn-rebuild-now").addEventListener("click", function () {
      spawnCtlWrite(["rebuild"], "nixos-rebuild switch");
    });

    $("btn-dry-build").addEventListener("click", function () {
      spawnCtlWrite(["dry-build"], "nixos-rebuild dry-build");
    });

    $("btn-rollback").addEventListener("click", function () {
      if (window.confirm("Revenir immédiatement à la génération NixOS précédente (nixos-rebuild switch --rollback) ?")) {
        spawnCtlWrite(["rollback"], "Rollback NixOS");
      }
    });

    $("btn-gc").addEventListener("click", function () {
      if (window.confirm("Supprimer TOUTES les anciennes générations NixOS et exécuter nix-collect-garbage -d ? Cette action est irréversible.")) {
        spawnCtlWrite(["gc"], "Garbage Collection (nix-collect-garbage -d)");
      }
    });

    $("btn-optimise").addEventListener("click", function () {
      spawnCtlWrite(["optimise-store"], "Optimisation Nix Store (déduplication hardlinks)");
    });

    $("btn-health-check").addEventListener("click", function () {
      spawnCtlWrite(["health-check"], "Exécution manuelle du Health-Checker NixOS");
    });

    $("btn-trigger-upgrade").addEventListener("click", function () {
      if (window.confirm("Déclencher immédiatement nixos-upgrade.service (mise à jour automatique du canal & rebuild) ?")) {
        spawnCtlWrite(["trigger-upgrade"], "Mise à jour automatique NixOS (nixos-upgrade)");
      }
    });

    $("btn-sync-nm").addEventListener("click", function () {
      spawnCtlWrite(["sync-networkmanager"], "Import déclaratif des profils NetworkManager");
    });

    $("form-add-pkg").addEventListener("submit", function (e) {
      e.preventDefault();
      var pkg = $("input-pkg-name").value.trim();
      if (!pkg) return;
      spawnCtlWrite(["pkg-add", pkg], "Installation déclarative de " + pkg).then(function () {
        $("input-pkg-name").value = "";
      });
    });

    $("btn-search-pkg").addEventListener("click", function () {
      var q = $("input-pkg-name").value.trim();
      var box = $("pkg-search-results");
      if (q.length < 2) {
        showAlert("Entrez au moins 2 caractères pour rechercher dans nixpkgs.", "error");
        return;
      }
      box.classList.remove("hidden");
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
          row.className = "search-item";
          var info = document.createElement("span");
          info.innerHTML = "<code></code> — ";
          info.firstChild.textContent = item.attr;
          info.appendChild(document.createTextNode(item.pname + " (" + (item.description || "") + ")"));
          var addBtn = document.createElement("button");
          addBtn.type = "button";
          addBtn.className = "btn btn-small btn-primary";
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

    $("form-firewall").addEventListener("submit", function (e) {
      e.preventDefault();
      var payload = JSON.stringify({
        enable: $("fw-enable").checked,
        tcp: $("fw-tcp").value,
        udp: $("fw-udp").value,
        trusted: $("fw-trusted").value
      });
      spawnCtlWrite(["fw-set", payload], "Mise à jour déclarative du pare-feu");
    });

    $("form-network").addEventListener("submit", function (e) {
      e.preventDefault();
      var payload = JSON.stringify({
        hostname: $("net-hostname").value.trim(),
        nameservers: $("net-dns").value
      });
      spawnCtlWrite(["net-set", payload], "Mise à jour déclarative du réseau");
    });

    $("form-add-mount").addEventListener("submit", function (e) {
      e.preventDefault();
      var payload = JSON.stringify({
        mountPoint: $("mnt-point").value.trim(),
        device: $("mnt-device").value.trim(),
        fsType: $("mnt-fstype").value,
        options: $("mnt-options").value.trim()
      });
      spawnCtlWrite(["mount-add", payload], "Ajout montage déclaratif " + $("mnt-point").value.trim()).then(function () {
        $("mnt-point").value = "";
        $("mnt-device").value = "";
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupTabs();
    setupEvents();
    updateAdminBadge();
    refreshAll();
    window.setInterval(function () {
      if (!busy) {
        spawnCtlRead(["telemetry"]).then(function (raw) {
          renderTelemetry(JSON.parse(raw));
        }).catch(function () {});
      }
    }, 10000);
  });
})();
