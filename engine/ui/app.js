(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    headerServiceDot: $("headerServiceDot"),
    headerServiceText: $("headerServiceText"),
    btnHeaderRestart: $("btnHeaderRestart"),

    viewport: $("viewport"),
    streamImg: $("streamImg"),
    standbyOverlay: $("standbyOverlay"),
    faceReticle: $("faceReticle"),
    aePill: $("aePill"),
    matchPill: $("matchPill"),
    matchPillText: $("matchPillText"),
    enrollHud: $("enrollHud"),
    enrollPrompt: $("enrollPrompt"),
    enrollCounter: $("enrollCounter"),
    enrollSegments: $("enrollSegments"),

    btnEnroll: $("btnEnroll"),
    btnLiveTest: $("btnLiveTest"),
    btnPreview: $("btnPreview"),
    btnDiagnostics: $("btnDiagnostics"),
    diagnosticsStrip: $("diagnosticsStrip"),
    diagnosticsText: $("diagnosticsText"),
    stageFeedback: $("stageFeedback"),
    stageFeedbackText: $("stageFeedbackText"),

    usernameInput: $("usernameInput"),
    profileSuggestions: $("profileSuggestions"),
    btnPassword: $("btnPassword"),
    btnRemove: $("btnRemove"),
    removeConfirmRow: $("removeConfirmRow"),
    removeConfirmText: $("removeConfirmText"),
    btnRemoveConfirm: $("btnRemoveConfirm"),
    btnRemoveCancel: $("btnRemoveCancel"),
    accountStatusLine: $("accountStatusLine"),

    modelsRow: $("modelsRow"),
    modelsSub: $("modelsSub"),
    btnDownloadModels: $("btnDownloadModels"),

    toggleOpenEyes: $("toggleOpenEyes"),
    toggleMotion: $("toggleMotion"),
    thresholdSlider: $("thresholdSlider"),
    thresholdValue: $("thresholdValue"),

    btnFeedSubtract: $("btnFeedSubtract"),
    btnFeedBright: $("btnFeedBright"),
    cameraSelectRow: $("cameraSelectRow"),
    cameraSelect: $("cameraSelect"),
    paneServiceDesc: $("paneServiceDesc"),
    btnPaneRestart: $("btnPaneRestart"),

    passwordModal: $("passwordModal"),
    btnPwClose: $("btnPwClose"),
    btnPwCancel: $("btnPwCancel"),
    passwordForm: $("passwordForm"),
    pwModalUser: $("pwModalUser"),
    pwDomainInput: $("pwDomainInput"),
    pwInput: $("pwInput"),
    pwConfirmInput: $("pwConfirmInput"),
    pwModalError: $("pwModalError"),
  };

  const state = {
    bootstrap: null,
    profiles: [],
    live: null,
    streamActiveUrl: "",
    diagnosticsVisible: false,
    pollTimer: null,
    serviceTimer: null,
    feedbackTimer: null,
    wasDownloading: false,
    wasEnrolling: false,
  };

  function api() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  function waitForPywebview() {
    if (api()) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const onReady = () => {
        window.removeEventListener("pywebviewready", onReady);
        resolve();
      };
      window.addEventListener("pywebviewready", onReady);
      const check = setInterval(() => {
        if (api()) {
          clearInterval(check);
          window.removeEventListener("pywebviewready", onReady);
          resolve();
        }
      }, 40);
    });
  }

  function showFeedback(message, tone = "neutral", durationMs = 4000) {
    if (state.feedbackTimer) {
      clearTimeout(state.feedbackTimer);
      state.feedbackTimer = null;
    }
    if (!message) {
      els.stageFeedback.classList.add("hidden");
      return;
    }
    els.stageFeedbackText.textContent = message;
    els.stageFeedback.dataset.tone = tone;
    els.stageFeedback.classList.remove("hidden");
    if (durationMs > 0) {
      state.feedbackTimer = setTimeout(() => {
        els.stageFeedback.classList.add("hidden");
      }, durationMs);
    }
  }

  function currentUsername() {
    return (els.usernameInput.value || "").trim();
  }

  function findProfile(username) {
    const clean = (username || "").trim().toLowerCase();
    if (!clean) return null;
    return state.profiles.find((p) => p.username.toLowerCase() === clean) || null;
  }

  function renderProfileDatalist() {
    els.profileSuggestions.innerHTML = "";
    for (const p of state.profiles) {
      const opt = document.createElement("option");
      opt.value = p.username;
      els.profileSuggestions.appendChild(opt);
    }
  }

  function renderAccountSection() {
    const user = currentUsername();
    const prof = findProfile(user);
    const mode = state.live ? state.live.mode : "standby";

    if (prof) {
      const samplesLabel = prof.sample_count === 1 ? "1 sample" : `${prof.sample_count} samples`;
      if (prof.has_password) {
        els.accountStatusLine.textContent = `${samplesLabel} · Password saved`;
        els.accountStatusLine.dataset.tone = "ok";
      } else {
        els.accountStatusLine.textContent = `${samplesLabel} · Password not set`;
        els.accountStatusLine.dataset.tone = "warn";
      }
      els.btnRemove.classList.remove("hidden");
    } else if (user) {
      els.accountStatusLine.textContent = "Not enrolled";
      els.accountStatusLine.dataset.tone = "neutral";
      els.btnRemove.classList.add("hidden");
      els.removeConfirmRow.classList.add("hidden");
    } else {
      els.accountStatusLine.textContent = "Enter a Windows username";
      els.accountStatusLine.dataset.tone = "warn";
      els.btnRemove.classList.add("hidden");
      els.removeConfirmRow.classList.add("hidden");
    }

    if (mode === "enroll") {
      els.btnEnroll.textContent = "Cancel";
    } else {
      els.btnEnroll.textContent = prof && prof.sample_count > 0 ? "Re-enroll" : "Enroll Face";
    }
  }

  function renderServiceStatus(status, configDirty) {
    const labels = {
      running: "Service active",
      stopped: "Service offline",
      not_installed: "Service not installed",
      unknown: "Service unknown",
    };
    const paneDescs = {
      running: configDirty ? "Restart required to apply settings" : "Running in background",
      stopped: "Installed, currently stopped",
      not_installed: "Install via scripts/install_service.ps1",
      unknown: "Unable to query Service Control Manager",
    };

    els.headerServiceDot.dataset.status = status || "unknown";
    els.headerServiceText.textContent = labels[status] || "Service unknown";
    els.paneServiceDesc.textContent = paneDescs[status] || paneDescs.unknown;

    if (configDirty && status === "running") {
      els.btnHeaderRestart.classList.remove("hidden");
      els.btnPaneRestart.textContent = "Apply & Restart";
      els.btnPaneRestart.classList.remove("hidden");
    } else if (status === "running" || status === "stopped") {
      els.btnHeaderRestart.classList.add("hidden");
      els.btnPaneRestart.textContent = "Restart";
      els.btnPaneRestart.classList.remove("hidden");
    } else {
      els.btnHeaderRestart.classList.add("hidden");
      els.btnPaneRestart.classList.add("hidden");
    }
  }

  function renderModelsRow(modelsReady, download) {
    if (modelsReady) {
      els.modelsRow.classList.add("hidden");
      els.btnDownloadModels.disabled = false;
      return;
    }
    els.modelsRow.classList.remove("hidden");
    if (download && download.active) {
      els.btnDownloadModels.disabled = true;
      els.btnDownloadModels.textContent = "Downloading...";
      if (download.total && download.total > 0) {
        const pct = Math.min(100, Math.round((download.downloaded / download.total) * 100));
        els.modelsSub.textContent = `${download.file || "ONNX"} · ${pct}%`;
      } else if (download.downloaded > 0) {
        const mb = (download.downloaded / (1024 * 1024)).toFixed(1);
        els.modelsSub.textContent = `${download.file || "ONNX"} · ${mb} MB`;
      } else {
        els.modelsSub.textContent = "Downloading YuNet & SFace...";
      }
    } else {
      els.btnDownloadModels.disabled = false;
      els.btnDownloadModels.textContent = "Download Models";
      els.modelsSub.textContent =
        download && download.error ? `Failed: ${download.error}` : "YuNet & SFace ONNX required";
    }
  }

  function renderFeedMode(mode) {
    const isBright = mode === "bright";
    els.btnFeedSubtract.classList.toggle("active", !isBright);
    els.btnFeedBright.classList.toggle("active", isBright);
  }

  function humanizeReason(reason) {
    const map = {
      illuminator_inactive: "IR illuminator inactive",
      missing_dark_frame: "Syncing IR frames",
      invalid_bbox: "Adjust position",
      overexposed_ae_settling: "Settling exposure",
      too_dark: "Too dark",
      low_ir_delta: "Screen detected",
      low_ir_ratio: "High ambient IR",
      face_too_small: "Move closer",
      flat_background_spoof: "Flat background",
      flat_plane_spoof: "Flat surface",
      waiting_blink_or_motion: "Blink or move head",
      eyes_closed: "Eyes closed",
      no_ir: "IR camera required",
    };
    return map[reason] || reason || "Blocked";
  }

  function updateReticlePosition(bboxNorm, tone) {
    if (!bboxNorm || bboxNorm.length !== 4) {
      els.faceReticle.classList.add("hidden");
      return;
    }

    const vpW = els.viewport.clientWidth;
    const vpH = els.viewport.clientHeight;
    if (!vpW || !vpH) {
      els.faceReticle.classList.add("hidden");
      return;
    }

    const natW = els.streamImg.naturalWidth || vpW;
    const natH = els.streamImg.naturalHeight || vpH;
    const scale = Math.min(vpW / natW, vpH / natH);
    const drawW = natW * scale;
    const drawH = natH * scale;
    const offsetX = (vpW - drawW) * 0.5;
    const offsetY = (vpH - drawH) * 0.5;

    const [nx, ny, nw, nh] = bboxNorm;
    els.faceReticle.style.left = `${(offsetX + nx * drawW).toFixed(1)}px`;
    els.faceReticle.style.top = `${(offsetY + ny * drawH).toFixed(1)}px`;
    els.faceReticle.style.width = `${Math.max(16, nw * drawW).toFixed(1)}px`;
    els.faceReticle.style.height = `${Math.max(16, nh * drawH).toFixed(1)}px`;
    els.faceReticle.dataset.tone = tone;
    els.faceReticle.classList.remove("hidden");
  }

  function renderLiveState(live) {
    state.live = live;
    const mode = live.mode || "standby";
    const active = Boolean(live.camera_active) && mode !== "standby";

    // Stream image & standby placeholder
    if (active) {
      const targetUrl = `${state.bootstrap.stream_url}`;
      if (!state.streamActiveUrl) {
        state.streamActiveUrl = `${targetUrl}&ts=${Date.now()}`;
        els.streamImg.src = state.streamActiveUrl;
      }
      els.streamImg.classList.remove("hidden");
      els.standbyOverlay.classList.add("hidden");
    } else {
      if (state.streamActiveUrl) {
        state.streamActiveUrl = "";
        els.streamImg.removeAttribute("src");
      }
      els.streamImg.classList.add("hidden");
      els.standbyOverlay.classList.remove("hidden");
      els.faceReticle.classList.add("hidden");
    }

    // Buttons state
    const prof = findProfile(currentUsername());
    if (mode === "enroll") {
      els.btnEnroll.textContent = "Cancel";
    } else {
      els.btnEnroll.textContent = prof && prof.sample_count > 0 ? "Re-enroll" : "Enroll Face";
    }
    els.btnLiveTest.textContent = mode === "test" ? "Stop Test" : "Test Live";
    els.btnLiveTest.classList.toggle("active", mode === "test");
    els.btnPreview.setAttribute("aria-pressed", mode === "preview" ? "true" : "false");

    // AE settling pill
    if (active && live.ae_settling) {
      els.aePill.classList.remove("hidden");
    } else {
      els.aePill.classList.add("hidden");
    }

    // Match pill & reticle tone
    let reticleTone = "neutral";
    if (active && mode === "test") {
      const m = live.match || {};
      const pct = m.score ? `${Math.round(m.score * 100)}%` : "";
      if (m.state === "verified") {
        reticleTone = "ok";
        els.matchPill.dataset.tone = "ok";
        els.matchPillText.textContent = `Verified · ${m.username} · ${pct}`;
      } else if (m.state === "blocked") {
        reticleTone = "blocked";
        els.matchPill.dataset.tone = "blocked";
        els.matchPillText.textContent = `Blocked · ${humanizeReason(m.detail)}`;
      } else if (m.state === "waiting") {
        reticleTone = "warn";
        els.matchPill.dataset.tone = "warn";
        els.matchPillText.textContent = humanizeReason(m.detail);
      } else if (m.state === "no_match") {
        reticleTone = "blocked";
        els.matchPill.dataset.tone = "blocked";
        els.matchPillText.textContent = pct ? `Not recognized · ${pct}` : "Not recognized";
      } else if (m.state === "no_profiles") {
        reticleTone = "warn";
        els.matchPill.dataset.tone = "warn";
        els.matchPillText.textContent = "No enrolled profile";
      } else {
        reticleTone = "neutral";
        els.matchPill.dataset.tone = "neutral";
        els.matchPillText.textContent = "Looking for face";
      }
      els.matchPill.classList.remove("hidden");
    } else {
      els.matchPill.classList.add("hidden");
      if (active && live.liveness) {
        if (live.liveness.passed) {
          reticleTone = "ok";
        } else if (live.liveness.reason === "overexposed_ae_settling") {
          reticleTone = "warn";
        } else if (live.bbox_norm) {
          reticleTone = "blocked";
        }
      }
    }

    if (active) {
      updateReticlePosition(live.bbox_norm, reticleTone);
    }

    // Enrollment HUD
    if (active && mode === "enroll") {
      const enr = live.enroll || { count: 0, target: 8, prompt: "" };
      els.enrollPrompt.textContent = enr.prompt || "Look straight at the camera";
      els.enrollCounter.textContent = `${enr.count} / ${enr.target}`;
      const segs = els.enrollSegments.querySelectorAll(".seg");
      segs.forEach((seg, idx) => {
        seg.classList.toggle("filled", idx < enr.count);
      });
      els.enrollHud.classList.remove("hidden");
      state.wasEnrolling = true;
    } else {
      els.enrollHud.classList.add("hidden");
      if (state.wasEnrolling) {
        state.wasEnrolling = false;
        const doneUser = live.enroll && live.enroll.completed_user;
        if (doneUser) {
          showFeedback(`Enrolled 8 IR samples for '${doneUser}'.`, "ok", 5000);
          refreshBootstrapData();
        }
      }
    }

    // Diagnostics strip
    if (state.diagnosticsVisible) {
      const liv = live.liveness || {};
      if (active && live.bbox_norm) {
        const deltaStr = Number(liv.delta || 0).toFixed(1);
        const bgStr = Number(liv.bg_ratio || 0).toFixed(2);
        const r3d = Number(liv.relief_3d || 0);
        const r3dStr = `${r3d >= 0 ? "+" : ""}${r3d.toFixed(2)}`;
        const eyesStr =
          liv.eyes_left !== null && liv.eyes_left !== undefined
            ? ` · Eyes ${Number(liv.eyes_left).toFixed(2)}/${Number(liv.eyes_right).toFixed(2)}`
            : "";
        els.diagnosticsText.textContent = `IR Δ ${deltaStr} · BG ${bgStr} · 3D ${r3dStr}${eyesStr} · ${liv.reason}`;
      } else if (active) {
        els.diagnosticsText.textContent = "IR Δ -- · BG -- · 3D -- · No face";
      } else {
        els.diagnosticsText.textContent = "IR Δ -- · BG -- · 3D -- · Standby";
      }
    }

    // Download & model readiness sync
    const dl = live.download || {};
    renderModelsRow(Boolean(live.models_ready), dl);
    if (state.wasDownloading && !dl.active && live.models_ready) {
      showFeedback("YuNet and SFace models ready.", "ok", 4000);
    }
    state.wasDownloading = Boolean(dl.active);

    if (live.error) {
      showFeedback(live.error, "error", 6000);
    }

    scheduleNextPoll(active || Boolean(dl.active));
  }

  function scheduleNextPoll(fast) {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
    }
    const interval = fast ? 85 : 1400;
    state.pollTimer = setTimeout(pollLiveState, interval);
  }

  async function pollLiveState() {
    const a = api();
    if (!a) return;
    try {
      const live = await a.get_state();
      renderLiveState(live);
    } catch (_err) {
      scheduleNextPoll(false);
    }
  }

  async function refreshBootstrapData() {
    const a = api();
    if (!a) return;
    const boot = await a.get_bootstrap();
    state.bootstrap = boot;
    state.profiles = boot.profiles || [];
    renderProfileDatalist();
    renderAccountSection();
    renderServiceStatus(boot.service_status, boot.config_dirty);
  }

  function applyBootstrap(boot) {
    state.bootstrap = boot;
    state.profiles = boot.profiles || [];

    if (!els.usernameInput.value) {
      els.usernameInput.value = boot.active_user || "";
    }
    renderProfileDatalist();
    renderAccountSection();

    const cfg = boot.config || {};
    els.toggleOpenEyes.checked = Boolean(cfg.require_open_eyes);
    els.toggleMotion.checked = Boolean(cfg.require_motion);
    const thresh = Number(cfg.threshold ?? 0.65).toFixed(2);
    els.thresholdSlider.value = thresh;
    els.thresholdValue.textContent = thresh;

    renderFeedMode(boot.feed_mode || "subtract");
    renderModelsRow(Boolean(boot.models_ready), null);
    renderServiceStatus(boot.service_status, boot.config_dirty);

    const cams = boot.cameras || [];
    if (cams.length > 1) {
      els.cameraSelect.innerHTML = "";
      for (const cam of cams) {
        const opt = document.createElement("option");
        opt.value = String(cam.index);
        opt.textContent = cam.label;
        els.cameraSelect.appendChild(opt);
      }
      els.cameraSelect.value = String(boot.selected_camera ?? 0);
      els.cameraSelectRow.classList.remove("hidden");
    } else {
      els.cameraSelectRow.classList.add("hidden");
    }
  }

  async function persistSecurityConfig() {
    const a = api();
    if (!a) return;
    const threshold = parseFloat(els.thresholdSlider.value);
    const requireMotion = Boolean(els.toggleMotion.checked);
    const requireOpenEyes = Boolean(els.toggleOpenEyes.checked);
    const res = await a.update_config(threshold, requireMotion, requireOpenEyes);
    if (res && res.ok) {
      renderServiceStatus(res.service_status, res.config_dirty);
    }
  }

  function openPasswordModal() {
    const user = currentUsername();
    if (!user) {
      showFeedback("Enter a Windows username first.", "warn");
      els.usernameInput.focus();
      return;
    }
    const prof = findProfile(user);
    els.pwModalUser.textContent = user;
    els.pwDomainInput.value = prof && prof.domain ? prof.domain : ".";
    els.pwInput.value = "";
    els.pwConfirmInput.value = "";
    els.pwModalError.classList.add("hidden");
    els.passwordModal.classList.remove("hidden");
    els.pwInput.focus();
  }

  function closePasswordModal() {
    els.pwInput.value = "";
    els.pwConfirmInput.value = "";
    els.pwModalError.classList.add("hidden");
    els.passwordModal.classList.add("hidden");
  }

  function wireEvents() {
    els.btnEnroll.addEventListener("click", async () => {
      const a = api();
      if (!a) return;
      const mode = state.live ? state.live.mode : "standby";
      if (mode === "enroll") {
        const res = await a.cancel_action();
        if (res && res.state) renderLiveState(res.state);
        showFeedback("Enrollment cancelled.", "neutral");
        return;
      }
      const user = currentUsername();
      if (!user) {
        showFeedback("Enter a Windows username first.", "warn");
        els.usernameInput.focus();
        return;
      }
      const res = await a.start_enroll(user);
      if (!res.ok) {
        showFeedback(res.error || "Unable to start enrollment.", "error");
      }
      if (res && res.state) renderLiveState(res.state);
    });

    els.btnLiveTest.addEventListener("click", async () => {
      const a = api();
      if (!a) return;
      const res = await a.toggle_live_test();
      if (!res.ok) {
        showFeedback(res.error || "Unable to start live test.", "error");
      }
      if (res && res.state) renderLiveState(res.state);
    });

    els.btnPreview.addEventListener("click", async () => {
      const a = api();
      if (!a) return;
      const res = await a.toggle_preview();
      if (!res.ok) {
        showFeedback(res.error || "Unable to toggle preview.", "error");
      }
      if (res && res.state) renderLiveState(res.state);
    });

    els.btnDiagnostics.addEventListener("click", () => {
      state.diagnosticsVisible = !state.diagnosticsVisible;
      els.btnDiagnostics.setAttribute("aria-pressed", state.diagnosticsVisible ? "true" : "false");
      els.diagnosticsStrip.classList.toggle("hidden", !state.diagnosticsVisible);
      if (state.live) renderLiveState(state.live);
    });

    els.usernameInput.addEventListener("input", () => {
      els.removeConfirmRow.classList.add("hidden");
      renderAccountSection();
    });

    els.btnPassword.addEventListener("click", openPasswordModal);
    els.btnPwClose.addEventListener("click", closePasswordModal);
    els.btnPwCancel.addEventListener("click", closePasswordModal);

    els.passwordModal.addEventListener("click", (ev) => {
      if (ev.target === els.passwordModal) closePasswordModal();
    });

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !els.passwordModal.classList.contains("hidden")) {
        closePasswordModal();
      }
    });

    els.passwordForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const a = api();
      if (!a) return;
      const user = currentUsername();
      const domain = (els.pwDomainInput.value || ".").trim() || ".";
      const pw = els.pwInput.value;
      const confirm = els.pwConfirmInput.value;

      if (!pw) {
        els.pwModalError.textContent = "Enter your Windows password.";
        els.pwModalError.classList.remove("hidden");
        return;
      }
      if (pw !== confirm) {
        els.pwModalError.textContent = "Passwords do not match.";
        els.pwModalError.classList.remove("hidden");
        return;
      }

      const res = await a.save_password(user, domain, pw);
      if (!res.ok) {
        els.pwModalError.textContent = res.error || "Failed to save password.";
        els.pwModalError.classList.remove("hidden");
        return;
      }
      state.profiles = res.profiles || [];
      renderProfileDatalist();
      renderAccountSection();
      closePasswordModal();
      showFeedback(`Saved DPAPI credentials for '${user}'.`, "ok");
    });

    els.btnRemove.addEventListener("click", () => {
      const user = currentUsername();
      if (!user) return;
      els.removeConfirmText.textContent = `Remove '${user}'?`;
      els.removeConfirmRow.classList.remove("hidden");
    });

    els.btnRemoveCancel.addEventListener("click", () => {
      els.removeConfirmRow.classList.add("hidden");
    });

    els.btnRemoveConfirm.addEventListener("click", async () => {
      const a = api();
      if (!a) return;
      const user = currentUsername();
      const res = await a.delete_profile(user);
      els.removeConfirmRow.classList.add("hidden");
      if (res && res.ok) {
        state.profiles = res.profiles || [];
        renderProfileDatalist();
        renderAccountSection();
        if (res.deleted) {
          showFeedback(`Removed profile '${user}'.`, "neutral");
        }
      }
    });

    els.btnDownloadModels.addEventListener("click", async () => {
      const a = api();
      if (!a) return;
      const res = await a.download_models();
      if (res && res.started) {
        state.wasDownloading = true;
        pollLiveState();
      }
    });

    els.toggleOpenEyes.addEventListener("change", persistSecurityConfig);
    els.toggleMotion.addEventListener("change", persistSecurityConfig);

    els.thresholdSlider.addEventListener("input", () => {
      els.thresholdValue.textContent = parseFloat(els.thresholdSlider.value).toFixed(2);
    });
    els.thresholdSlider.addEventListener("change", persistSecurityConfig);

    const setFeed = async (mode) => {
      const a = api();
      renderFeedMode(mode);
      if (a) await a.set_feed_mode(mode);
    };
    els.btnFeedSubtract.addEventListener("click", () => setFeed("subtract"));
    els.btnFeedBright.addEventListener("click", () => setFeed("bright"));

    els.cameraSelect.addEventListener("change", async () => {
      const a = api();
      if (!a) return;
      const idx = parseInt(els.cameraSelect.value, 10);
      const res = await a.select_camera(idx);
      if (res && !res.ok && res.error) {
        showFeedback(res.error, "error");
      }
    });

    const handleRestart = async () => {
      const a = api();
      if (!a) return;
      const res = await a.restart_service();
      renderServiceStatus(res.service_status, res.config_dirty);
      if (res.ok) {
        showFeedback("Service restart requested.", "ok");
      } else if (res.error) {
        showFeedback(res.error, "warn");
      }
    };
    els.btnHeaderRestart.addEventListener("click", handleRestart);
    els.btnPaneRestart.addEventListener("click", handleRestart);

    window.addEventListener("resize", () => {
      if (state.live && state.live.camera_active && state.live.bbox_norm) {
        updateReticlePosition(state.live.bbox_norm, els.faceReticle.dataset.tone || "neutral");
      }
    });

    window.addEventListener("focus", async () => {
      const a = api();
      if (!a) return;
      try {
        const res = await a.refresh_service_status();
        if (res && res.ok) renderServiceStatus(res.service_status, res.config_dirty);
      } catch (_err) {
        // Ignore transient focus errors
      }
    });
  }

  async function init() {
    await waitForPywebview();
    wireEvents();
    const a = api();
    const boot = await a.get_bootstrap();
    applyBootstrap(boot);
    const live = await a.get_state();
    renderLiveState(live);

    state.serviceTimer = setInterval(async () => {
      const currApi = api();
      if (!currApi) return;
      try {
        const res = await currApi.refresh_service_status();
        if (res && res.ok) renderServiceStatus(res.service_status, res.config_dirty);
      } catch (_err) {
        // Ignore
      }
    }, 8000);
  }

  init().catch((err) => {
    showFeedback(`Initialization error: ${err}`, "error", 0);
  });
})();
