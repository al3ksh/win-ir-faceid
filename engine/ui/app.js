(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const CHECK_SVG =
    '<svg class="dropdown-check" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.8 7.3 5.7 10.2 11.2 4.2"/></svg>';

  const NO_IR_ERROR_INFO = {
    code: "no_ir_camera",
    title: "No infrared camera detected",
    causes: [
      "This device may not have a Windows Hello IR camera.",
      "Check Device Manager -> Cameras and ensure the IR sensor is enabled.",
      "Verify Windows Settings -> Privacy & security -> Camera allows desktop apps.",
    ],
    detail: "",
  };

  const els = {
    appShell: document.querySelector(".app-shell"),
    settingsPane: document.querySelector(".settings-pane"),

    btnBrandRepo: $("btnBrandRepo"),
    btnVersion: $("btnVersion"),
    headerServiceDot: $("headerServiceDot"),
    headerServiceText: $("headerServiceText"),
    btnHeaderRestart: $("btnHeaderRestart"),

    viewport: $("viewport"),
    streamImg: $("streamImg"),
    standbyOverlay: $("standbyOverlay"),
    standbyTitle: $("standbyTitle"),
    stageErrorOverlay: $("stageErrorOverlay"),
    stageErrorTitle: $("stageErrorTitle"),
    stageErrorCauses: $("stageErrorCauses"),
    stageErrorDetail: $("stageErrorDetail"),
    btnErrorRetry: $("btnErrorRetry"),
    btnErrorRescan: $("btnErrorRescan"),
    btnErrorDismiss: $("btnErrorDismiss"),
    faceReticle: $("faceReticle"),
    enrollStageGuide: $("enrollStageGuide"),
    stageGuideProgress: $("stageGuideProgress"),
    aePill: $("aePill"),
    matchPill: $("matchPill"),
    matchPillText: $("matchPillText"),
    enrollHud: $("enrollHud"),
    enrollPrompt: $("enrollPrompt"),
    enrollSub: $("enrollSub"),
    poseGuide: $("poseGuide"),
    poseRing: $("poseRing"),
    enrollCounter: $("enrollCounter"),
    enrollSegments: $("enrollSegments"),

    btnEnroll: $("btnEnroll"),
    btnLiveTest: $("btnLiveTest"),
    btnPreview: $("btnPreview"),
    btnDiagnostics: $("btnDiagnostics"),
    diagnosticsDrawer: $("diagnosticsDrawer"),
    diagnosticsStrip: $("diagnosticsStrip"),
    diagnosticsText: $("diagnosticsText"),
    feedbackDrawer: $("feedbackDrawer"),
    stageFeedback: $("stageFeedback"),
    stageFeedbackText: $("stageFeedbackText"),

    usernameInput: $("usernameInput"),
    profileSuggestions: $("profileSuggestions"),
    btnPassword: $("btnPassword"),
    btnRemove: $("btnRemove"),
    removeConfirmDrawer: $("removeConfirmDrawer"),
    removeConfirmRow: $("removeConfirmRow"),
    removeConfirmText: $("removeConfirmText"),
    btnRemoveConfirm: $("btnRemoveConfirm"),
    btnRemoveCancel: $("btnRemoveCancel"),
    accountStatusLine: $("accountStatusLine"),
    looksList: $("looksList"),

    modelsRow: $("modelsRow"),
    modelsSub: $("modelsSub"),
    btnDownloadModels: $("btnDownloadModels"),

    toggleOpenEyes: $("toggleOpenEyes"),
    toggleMotion: $("toggleMotion"),
    thresholdSlider: $("thresholdSlider"),
    thresholdValue: $("thresholdValue"),
    scoreStats: $("scoreStats"),
    scoreRange: $("scoreRange"),
    scoreMedian: $("scoreMedian"),
    scoreThresh: $("scoreThresh"),
    scoreStatsText: $("scoreStatsText"),
    bitlockerNotice: $("bitlockerNotice"),
    bitlockerSub: $("bitlockerSub"),
    btnBitlockerSettings: $("btnBitlockerSettings"),

    feedSegThumb: $("feedSegThumb"),
    btnFeedSubtract: $("btnFeedSubtract"),
    btnFeedBright: $("btnFeedBright"),
    cameraSelectRow: $("cameraSelectRow"),
    cameraDropdown: $("cameraDropdown"),
    cameraDropdownBtn: $("cameraDropdownBtn"),
    cameraDropdownLabel: $("cameraDropdownLabel"),
    cameraDropdownMenu: $("cameraDropdownMenu"),
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

    updateModal: $("updateModal"),
    btnUpdateClose: $("btnUpdateClose"),
    updateVersionLine: $("updateVersionLine"),
    updateMetaLine: $("updateMetaLine"),
    updateNotesBox: $("updateNotesBox"),
    updateNotesList: $("updateNotesList"),
    updateProgressRow: $("updateProgressRow"),
    updateProgressLabel: $("updateProgressLabel"),
    updateProgressPct: $("updateProgressPct"),
    updateProgressFill: $("updateProgressFill"),
    updateModalError: $("updateModalError"),
    btnUpdateLater: $("btnUpdateLater"),
    btnUpdateGithub: $("btnUpdateGithub"),
    btnUpdateInstall: $("btnUpdateInstall"),
  };

  const state = {
    appReady: false,
    bootstrap: null,
    profiles: [],
    live: null,
    streamActiveUrl: "",
    streamFrameReady: false,
    clearSrcTimer: null,
    diagnosticsVisible: false,
    pollTimer: null,
    serviceTimer: null,
    feedbackTimer: null,
    wasDownloading: false,
    wasEnrolling: false,
    cameras: [],
    selectedCameraIdx: 0,
    hasIrCamera: true,
    dropdownOpen: false,
    dropdownFocusIdx: -1,
    lastAttemptedMode: null,
    lastEnrollArgs: null,
    startupNoIrDismissed: false,
    currentErrorInfo: null,
    isRescanning: false,
    updateInfo: null,
    testScores: [],
    lastMatchSeq: 0,
    wasTesting: false,
  };

  // ---- Live-test score distribution (helps pick the match threshold) ----
  const SCORE_SCALE_MIN = 0.3;
  const SCORE_SCALE_MAX = 1.0;
  const MAX_TEST_SCORES = 900;

  function scorePos(v) {
    const t = (v - SCORE_SCALE_MIN) / (SCORE_SCALE_MAX - SCORE_SCALE_MIN);
    return Math.min(1, Math.max(0, t)) * 100;
  }

  function collectTestScore(mode, active, match) {
    const testing = active && mode === "test";
    if (testing && !state.wasTesting) {
      state.testScores = [];
      state.lastMatchSeq = 0;
    }
    state.wasTesting = testing;
    if (!testing || !match) return;
    const seq = Number(match.seq || 0);
    const score = Number(match.score || 0);
    if (!seq || seq === state.lastMatchSeq || !match.username || !(score > 0)) return;
    state.lastMatchSeq = seq;
    state.testScores.push(score);
    if (state.testScores.length > MAX_TEST_SCORES) state.testScores.shift();
    renderScoreStats();
  }

  function renderScoreStats() {
    const xs = state.testScores;
    if (!xs.length) {
      els.scoreStats.classList.add("hidden");
      return;
    }
    const sorted = [...xs].sort((a, b) => a - b);
    const n = sorted.length;
    // 5th percentile instead of the raw minimum: a few transitional frames (sitting
    // down, head turned away, face half out of frame) should not dominate. The max stays
    // raw, because for someone else's face a single high frame is what matters.
    const low = sorted[Math.min(n - 1, Math.floor(n * 0.05))];
    const max = sorted[n - 1];
    const median = n % 2 ? sorted[(n - 1) / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
    const thr = parseFloat(els.thresholdSlider.value);
    const below = sorted.filter((v) => v < thr).length;
    const pctBelow = Math.round((below / n) * 100);

    const left = scorePos(low);
    els.scoreRange.style.left = `${left}%`;
    els.scoreRange.style.width = `max(2px, ${scorePos(max) - left}%)`;
    els.scoreRange.dataset.tone = low < thr ? "warn" : "ok";
    els.scoreMedian.style.left = `${scorePos(median)}%`;
    els.scoreThresh.style.left = `${scorePos(thr)}%`;

    els.scoreStatsText.textContent =
      `Test: ${n} frames · low (5%) ${low.toFixed(2)} · median ${median.toFixed(2)} · max ${max.toFixed(2)} · ` +
      `${pctBelow}% below ${thr.toFixed(2)}. ` +
      "Your own face should sit right of the red line; anyone else's should stay left of it.";
    els.scoreStats.classList.remove("hidden");
  }

  function api() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  // pywebview creates window.pywebview.api before it injects the Python methods (and fires
  // "pywebviewready" only afterwards); on slow machines polling can see the empty object first.
  function apiReady() {
    const a = api();
    return !!a && typeof a.get_bootstrap === "function" && typeof a.get_state === "function";
  }

  function waitForPywebview() {
    if (apiReady()) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      let check = null;
      const done = () => {
        clearInterval(check);
        window.removeEventListener("pywebviewready", onReady);
        resolve();
      };
      const onReady = () => {
        if (apiReady()) done();
      };
      window.addEventListener("pywebviewready", onReady);
      check = setInterval(onReady, 40);
    });
  }

  function markAppReady() {
    if (state.appReady) return;
    state.appReady = true;
    positionSegThumb(false);
    if (els.appShell) {
      els.appShell.dataset.ready = "true";
    }
  }

  function setDrawerOpen(drawerEl, open) {
    if (!drawerEl) return;
    drawerEl.dataset.open = open ? "true" : "false";
    drawerEl.setAttribute("aria-hidden", open ? "false" : "true");
  }

  function setVisible(el, visible) {
    if (!el) return;
    const val = visible ? "true" : "false";
    if (el.dataset.visible !== val) {
      el.dataset.visible = val;
    }
    el.setAttribute("aria-hidden", visible ? "false" : "true");
  }

  function showFeedback(message, tone = "neutral", durationMs = 4000) {
    if (state.feedbackTimer) {
      clearTimeout(state.feedbackTimer);
      state.feedbackTimer = null;
    }
    if (!message) {
      setDrawerOpen(els.feedbackDrawer, false);
      return;
    }
    els.stageFeedbackText.textContent = message;
    els.stageFeedback.dataset.tone = tone;
    setDrawerOpen(els.feedbackDrawer, true);
    if (durationMs > 0) {
      state.feedbackTimer = setTimeout(() => {
        setDrawerOpen(els.feedbackDrawer, false);
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

  function updateEnrollButton(mode, prof) {
    if (mode === "enroll") {
      els.btnEnroll.textContent = "Cancel";
      els.btnEnroll.removeAttribute("title");
    } else if (prof && prof.sample_count > 0) {
      els.btnEnroll.textContent = "Add Appearance";
      els.btnEnroll.title = "Add another appearance, e.g. with glasses";
    } else {
      els.btnEnroll.textContent = "Enroll Face";
      els.btnEnroll.removeAttribute("title");
    }
  }

  function renderAccountSection() {
    const user = currentUsername();
    const prof = findProfile(user);
    const mode = state.live ? state.live.mode : "standby";

    if (prof) {
      let statusPrefix;
      if (prof.sample_count > 0) {
        const samplesLabel = prof.sample_count === 1 ? "1 sample" : `${prof.sample_count} samples`;
        const lookCount = (prof.looks || []).length;
        if (lookCount > 1) {
          statusPrefix = `Enrolled · ${samplesLabel} (${lookCount} looks)`;
        } else {
          statusPrefix = `Enrolled · ${samplesLabel}`;
        }
      } else {
        statusPrefix = "Not enrolled";
      }

      if (prof.has_password) {
        els.accountStatusLine.textContent = `${statusPrefix} · Password saved`;
        els.accountStatusLine.dataset.tone = "ok";
      } else {
        els.accountStatusLine.textContent = `${statusPrefix} · Password not set`;
        els.accountStatusLine.dataset.tone = "warn";
      }
      els.btnRemove.classList.remove("hidden");
      renderLooksList(user, prof.looks || []);
    } else if (user) {
      els.accountStatusLine.textContent = "Not enrolled";
      els.accountStatusLine.dataset.tone = "neutral";
      els.btnRemove.classList.add("hidden");
      setDrawerOpen(els.removeConfirmDrawer, false);
    } else {
      els.accountStatusLine.textContent = "Enter a Windows username";
      els.accountStatusLine.dataset.tone = "warn";
      els.btnRemove.classList.add("hidden");
      setDrawerOpen(els.removeConfirmDrawer, false);
    }
    if (!prof) renderLooksList(user, []);

    updateEnrollButton(mode, prof);
  }

  function formatLookDate(iso) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "Unknown date";
    return d.toLocaleString(undefined, {
      day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  function renderLooksList(user, looks) {
    const list = els.looksList;
    if (!list) return;
    // Only rebuild when the looks actually change (this runs on every poll tick).
    const key = user + "|" + looks.map((l) => `${l.id}:${l.count}`).join(",");
    if (list.dataset.key === key) return;
    list.dataset.key = key;
    list.textContent = "";
    if (looks.length === 0) {
      list.classList.add("hidden");
      return;
    }
    list.classList.remove("hidden");
    const single = looks.length <= 1;
    looks.forEach((look, i) => {
      const li = document.createElement("li");
      li.className = "look-row";
      const label = document.createElement("span");
      label.className = "look-label";
      const name = document.createElement("span");
      name.className = "look-name";
      name.textContent = `Look ${i + 1}`;
      const meta = document.createElement("span");
      meta.className = "look-meta";
      const samples = look.count === 1 ? "1 sample" : `${look.count} samples`;
      meta.textContent = `${formatLookDate(look.added_at)} · ${samples}`;
      label.append(name, meta);

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-subtle btn-xs btn-danger-ghost look-delete";
      btn.textContent = "Delete";
      btn.disabled = single;
      btn.title = single ? "Your only look — use Remove to delete the whole profile" : "Delete this look";
      let disarmTimer = null;
      btn.addEventListener("click", async () => {
        if (btn.dataset.armed !== "true") {
          btn.dataset.armed = "true";
          btn.textContent = "Confirm";
          disarmTimer = setTimeout(() => {
            btn.dataset.armed = "false";
            btn.textContent = "Delete";
          }, 3000);
          return;
        }
        clearTimeout(disarmTimer);
        const a = api();
        if (!a) return;
        btn.disabled = true;
        const res = await a.delete_look(user, look.id);
        if (res) state.profiles = res.profiles || state.profiles;
        list.dataset.key = "";
        renderProfileDatalist();
        renderAccountSection();
        if (res && res.ok) {
          showFeedback(`Deleted look ${i + 1}.`, "neutral");
        } else if (res && res.error) {
          showFeedback(res.error, "error");
        }
      });
      li.append(label, btn);
      list.appendChild(li);
    });
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

  function renderBitlockerNotice(diskEncryption) {
    if (!els.bitlockerNotice) return;
    if (!diskEncryption || diskEncryption.status !== "unencrypted") {
      els.bitlockerNotice.classList.add("hidden");
      return;
    }
    if (els.bitlockerSub) {
      els.bitlockerSub.textContent =
        diskEncryption.detail === "suspended"
          ? "BitLocker protection is suspended on drive C:."
          : "Turn on BitLocker to protect stored credentials offline.";
    }
    els.bitlockerNotice.classList.remove("hidden");
  }

  function positionSegThumb(animate = true) {
    const thumb = els.feedSegThumb;
    const activeBtn = els.btnFeedBright.classList.contains("active")
      ? els.btnFeedBright
      : els.btnFeedSubtract;
    if (!thumb || !activeBtn) return;

    const width = activeBtn.offsetWidth;
    const left = activeBtn.offsetLeft;
    if (!width) return;

    if (!animate) {
      thumb.classList.add("no-transition");
    }
    thumb.style.width = `${width}px`;
    thumb.style.transform = `translateX(${left}px)`;
    if (!animate) {
      void thumb.offsetWidth;
      thumb.classList.remove("no-transition");
    }
  }

  function renderFeedMode(mode) {
    const isBright = mode === "bright";
    els.btnFeedSubtract.classList.toggle("active", !isBright);
    els.btnFeedBright.classList.toggle("active", isBright);
    positionSegThumb(state.appReady);
  }

  /* Custom Monochrome Camera Dropdown */
  function renderCameraDropdown(cameras, selectedIdx) {
    state.cameras = Array.isArray(cameras) ? cameras : [];
    state.selectedCameraIdx = Number.isFinite(Number(selectedIdx)) ? Number(selectedIdx) : 0;

    if (state.cameras.length <= 1) {
      closeCameraDropdown(false);
      els.cameraSelectRow.classList.add("hidden");
      els.cameraDropdownMenu.innerHTML = "";
      return;
    }

    els.cameraDropdownMenu.innerHTML = "";
    let activeCam = state.cameras[0];

    state.cameras.forEach((cam, pos) => {
      const isSelected = Number(cam.index) === state.selectedCameraIdx;
      if (isSelected) activeCam = cam;

      const opt = document.createElement("div");
      opt.className = "dropdown-option";
      opt.id = `cameraOption-${cam.index}`;
      opt.setAttribute("role", "option");
      opt.setAttribute("tabindex", "-1");
      opt.setAttribute("aria-selected", isSelected ? "true" : "false");
      opt.dataset.index = String(cam.index);
      opt.dataset.pos = String(pos);

      const labelSpan = document.createElement("span");
      labelSpan.className = "dropdown-option-label";
      labelSpan.textContent = cam.label;
      opt.appendChild(labelSpan);

      if (cam.is_ir === true) {
        const badge = document.createElement("span");
        badge.className = "dropdown-ir-badge";
        badge.textContent = "IR";
        opt.appendChild(badge);
      }

      opt.insertAdjacentHTML("beforeend", CHECK_SVG);

      opt.addEventListener("mouseenter", () => {
        setDropdownFocus(pos);
      });
      opt.addEventListener("click", (ev) => {
        ev.stopPropagation();
        selectCameraOption(Number(cam.index));
      });

      els.cameraDropdownMenu.appendChild(opt);
    });

    els.cameraDropdownLabel.textContent = activeCam ? activeCam.label : "Select camera";
    els.cameraSelectRow.classList.remove("hidden");
  }

  function updateDropdownSelectionUI(selectedIdx) {
    state.selectedCameraIdx = Number(selectedIdx);
    let activeCam = null;
    const options = els.cameraDropdownMenu.querySelectorAll(".dropdown-option");
    options.forEach((opt) => {
      const idx = Number(opt.dataset.index);
      const selected = idx === state.selectedCameraIdx;
      opt.setAttribute("aria-selected", selected ? "true" : "false");
      if (selected) {
        activeCam = state.cameras.find((c) => Number(c.index) === idx) || null;
      }
    });
    if (activeCam) {
      els.cameraDropdownLabel.textContent = activeCam.label;
    }
  }

  function setDropdownFocus(pos) {
    const options = els.cameraDropdownMenu.querySelectorAll(".dropdown-option");
    if (!options.length) {
      state.dropdownFocusIdx = -1;
      els.cameraDropdownBtn.removeAttribute("aria-activedescendant");
      return;
    }
    const clamped = ((pos % options.length) + options.length) % options.length;
    state.dropdownFocusIdx = clamped;
    options.forEach((opt, idx) => {
      opt.classList.toggle("is-focused", idx === clamped);
    });
    const activeOpt = options[clamped];
    if (activeOpt) {
      els.cameraDropdownBtn.setAttribute("aria-activedescendant", activeOpt.id);
      activeOpt.scrollIntoView({ block: "nearest" });
    }
  }

  function positionCameraDropdownMenu() {
    const btn = els.cameraDropdownBtn;
    const menu = els.cameraDropdownMenu;
    if (!btn || !menu) return;

    const rect = btn.getBoundingClientRect();
    const minW = Math.max(rect.width, 200);
    const maxW = Math.min(296, Math.max(184, window.innerWidth - 24));
    menu.style.minWidth = `${minW}px`;
    menu.style.maxWidth = `${maxW}px`;

    const menuHeight = Math.min(240, menu.scrollHeight || state.cameras.length * 32 + 10);
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;

    // Open upward when near the bottom of the settings pane to prevent clipping.
    const openUpward = spaceBelow < menuHeight + 12 || (spaceBelow < 160 && spaceAbove >= spaceBelow);
    const rightOffset = Math.max(8, window.innerWidth - rect.right);

    menu.style.right = `${rightOffset}px`;
    menu.style.left = "auto";

    if (openUpward) {
      menu.dataset.placement = "top";
      menu.style.bottom = `${window.innerHeight - rect.top + 6}px`;
      menu.style.top = "auto";
    } else {
      menu.dataset.placement = "bottom";
      menu.style.top = `${rect.bottom + 6}px`;
      menu.style.bottom = "auto";
    }
  }

  function openCameraDropdown() {
    if (state.dropdownOpen || state.cameras.length <= 1) return;
    state.dropdownOpen = true;
    positionCameraDropdownMenu();
    els.cameraDropdownMenu.dataset.open = "true";
    els.cameraDropdownMenu.setAttribute("aria-hidden", "false");
    els.cameraDropdownBtn.setAttribute("aria-expanded", "true");

    const selectedPos = Math.max(
      0,
      state.cameras.findIndex((c) => Number(c.index) === state.selectedCameraIdx)
    );
    setDropdownFocus(selectedPos);
  }

  function closeCameraDropdown(restoreFocus = false) {
    if (!state.dropdownOpen) return;
    state.dropdownOpen = false;
    state.dropdownFocusIdx = -1;
    els.cameraDropdownMenu.dataset.open = "false";
    els.cameraDropdownMenu.setAttribute("aria-hidden", "true");
    els.cameraDropdownBtn.setAttribute("aria-expanded", "false");
    els.cameraDropdownBtn.removeAttribute("aria-activedescendant");
    els.cameraDropdownMenu
      .querySelectorAll(".dropdown-option.is-focused")
      .forEach((opt) => opt.classList.remove("is-focused"));
    if (restoreFocus) {
      els.cameraDropdownBtn.focus();
    }
  }

  async function selectCameraOption(idx) {
    const prevIdx = state.selectedCameraIdx;
    closeCameraDropdown(true);
    if (idx === prevIdx) return;

    updateDropdownSelectionUI(idx);
    const a = api();
    if (!a) return;
    const res = await a.select_camera(idx);
    if (res && !res.ok) {
      updateDropdownSelectionUI(prevIdx);
      if (res.state && res.state.error_info) {
        renderLiveState(res.state);
      } else if (res.error_info) {
        renderStageErrorOverlay(res.error_info);
      } else if (res.error) {
        showFeedback(res.error, "error");
      }
    }
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
      els.faceReticle.dataset.visible = "false";
      return;
    }

    const vpW = els.viewport.clientWidth;
    const vpH = els.viewport.clientHeight;
    if (!vpW || !vpH) {
      els.faceReticle.dataset.visible = "false";
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
    const x = offsetX + nx * drawW;
    const y = offsetY + ny * drawH;
    const w = Math.max(16, nw * drawW);
    const h = Math.max(16, nh * drawH);

    const wasVisible = els.faceReticle.dataset.visible === "true";
    if (!wasVisible) {
      els.faceReticle.classList.add("no-pos-transition");
    }

    els.faceReticle.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0)`;
    els.faceReticle.style.width = `${w.toFixed(1)}px`;
    els.faceReticle.style.height = `${h.toFixed(1)}px`;
    els.faceReticle.dataset.tone = tone;

    if (!wasVisible) {
      void els.faceReticle.offsetWidth;
      els.faceReticle.classList.remove("no-pos-transition");
    }
    els.faceReticle.dataset.visible = "true";
  }

  function revealStreamFrame() {
    if (!state.streamActiveUrl) return;
    state.streamFrameReady = true;
    els.streamImg.dataset.visible = "true";
    els.standbyOverlay.dataset.visible = "false";
    els.standbyOverlay.dataset.starting = "false";
    if (els.enrollStageGuide && state.live && state.live.mode === "enroll") {
      els.enrollStageGuide.dataset.visible = "true";
    }
  }

  function renderStageErrorOverlay(errorInfo) {
    if (!errorInfo) {
      state.currentErrorInfo = null;
      els.stageErrorOverlay.dataset.code = "";
      setVisible(els.stageErrorOverlay, false);
      return;
    }

    state.currentErrorInfo = errorInfo;
    const code = errorInfo.code || "sensor_error";
    const title = (errorInfo.title || "IR sensor error").trim();
    const detail = (errorInfo.detail || "").trim();
    const causes = Array.isArray(errorInfo.causes) ? errorInfo.causes : [];

    els.stageErrorOverlay.dataset.code = code;
    els.stageErrorTitle.textContent = title;

    els.stageErrorCauses.innerHTML = "";
    for (const cause of causes) {
      const li = document.createElement("li");
      li.textContent = cause;
      els.stageErrorCauses.appendChild(li);
    }

    if (detail && detail.toLowerCase() !== title.toLowerCase()) {
      els.stageErrorDetail.textContent = detail;
      els.stageErrorDetail.classList.remove("hidden");
    } else {
      els.stageErrorDetail.textContent = "";
      els.stageErrorDetail.classList.add("hidden");
    }

    const showRetry = Boolean(state.lastAttemptedMode) || code !== "no_ir_camera";
    els.btnErrorRetry.classList.toggle("hidden", !showRetry);
    els.btnErrorRescan.classList.toggle("btn-secondary", !showRetry);
    els.btnErrorRescan.classList.toggle("btn-ghost", showRetry);

    els.standbyOverlay.dataset.starting = "false";
    els.standbyOverlay.dataset.visible = "false";
    setVisible(els.stageErrorOverlay, true);
  }

  function renderLiveState(live) {
    state.live = live;
    const mode = live.mode || "standby";
    const active = Boolean(live.camera_active) && mode !== "standby";

    // Stream image & standby / error cross-fade (wait for first real frame before revealing)
    if (active) {
      renderStageErrorOverlay(null);
      if (state.clearSrcTimer) {
        clearTimeout(state.clearSrcTimer);
        state.clearSrcTimer = null;
      }
      const targetUrl = `${state.bootstrap.stream_url}`;
      if (!state.streamActiveUrl) {
        state.streamFrameReady = false;
        els.standbyTitle.textContent = "Starting IR sensor...";
        els.standbyOverlay.dataset.starting = "true";
        els.standbyOverlay.dataset.visible = "true";
        els.streamImg.dataset.visible = "false";
        state.streamActiveUrl = `${targetUrl}&ts=${Date.now()}`;
        els.streamImg.src = state.streamActiveUrl;
      }
      if (state.streamFrameReady || (els.streamImg.complete && els.streamImg.naturalWidth > 0)) {
        revealStreamFrame();
      }
    } else {
      state.streamFrameReady = false;
      els.streamImg.dataset.visible = "false";
      els.standbyTitle.textContent = "IR sensor on standby";
      els.standbyOverlay.dataset.starting = "false";
      els.faceReticle.dataset.visible = "false";

      if (live.error_info) {
        renderStageErrorOverlay(live.error_info);
      } else if (!state.hasIrCamera && !state.startupNoIrDismissed) {
        renderStageErrorOverlay(NO_IR_ERROR_INFO);
      } else {
        renderStageErrorOverlay(null);
        els.standbyOverlay.dataset.visible = "true";
      }

      if (state.streamActiveUrl) {
        state.streamActiveUrl = "";
        if (state.clearSrcTimer) clearTimeout(state.clearSrcTimer);
        state.clearSrcTimer = setTimeout(() => {
          state.clearSrcTimer = null;
          if (!state.streamActiveUrl) {
            els.streamImg.removeAttribute("src");
          }
        }, 270);
      }
    }

    // Buttons state
    collectTestScore(mode, active, live.match);

    const prof = findProfile(currentUsername());
    updateEnrollButton(mode, prof);
    els.btnLiveTest.textContent = mode === "test" ? "Stop Test" : "Test Live";
    els.btnLiveTest.classList.toggle("active", mode === "test");
    els.btnPreview.setAttribute("aria-pressed", mode === "preview" ? "true" : "false");

    // AE settling pill
    setVisible(els.aePill, active && Boolean(live.ae_settling));

    // Match pill & reticle tone
    let reticleTone = "neutral";
    if (active && mode === "test") {
      const m = live.match || {};
      const pct = m.score ? Number(m.score).toFixed(2) : "";
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
      setVisible(els.matchPill, true);
    } else {
      setVisible(els.matchPill, false);
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

    if (active && mode !== "enroll") {
      updateReticlePosition(live.bbox_norm, reticleTone);
    } else {
      els.faceReticle.dataset.visible = "false";
    }

    // Enrollment HUD & On-Viewport Stage Guide
    if (active && mode === "enroll") {
      const enr = live.enroll || { count: 0, target: 7, prompt: "" };
      const promptText = enr.prompt || "Look straight at the camera";
      if (els.enrollPrompt.textContent !== promptText) {
        els.enrollPrompt.textContent = promptText;
        // Re-trigger the fade so a new instruction is noticeable.
        els.enrollPrompt.classList.remove("prompt-enter");
        void els.enrollPrompt.offsetWidth;
        els.enrollPrompt.classList.add("prompt-enter");
      }
      const readyIn = Number(enr.ready_in) || 0;
      const dwell = Number(enr.dwell) || 0;
      const justCaptured = Boolean(enr.just_captured);
      const pose = enr.pose || "center";
      els.enrollCounter.textContent = `${enr.count} / ${enr.target}`;
      const holdPct = dwell > 0 ? Math.round(100 * (1 - Math.min(1, readyIn / dwell))) : 100;
      let sub;
      if (justCaptured) {
        sub = `Captured · Next: ${promptText}`;
      } else if (!enr.face_ok) {
        sub = "Waiting for your face…";
      } else if (enr.count === 0 && holdPct < 40) {
        sub = "Get ready";
      } else {
        sub = `Hold still… ${Math.max(1, Math.ceil(readyIn))}`;
      }
      els.enrollSub.textContent = sub;
      els.enrollSub.dataset.tone = justCaptured || enr.face_ok ? "ok" : "warn";
      els.poseGuide.dataset.pose = pose;
      els.poseGuide.dataset.face = justCaptured || enr.face_ok ? "ok" : "none";
      const ringPct = justCaptured ? 100 : holdPct;
      els.poseRing.style.strokeDashoffset = String(100 - ringPct);
      if (els.enrollStageGuide) {
        els.enrollStageGuide.dataset.visible = state.streamFrameReady ? "true" : "false";
        els.enrollStageGuide.dataset.pose = pose;
        els.enrollStageGuide.dataset.face = enr.face_ok ? "ok" : "none";
        els.enrollStageGuide.dataset.captured = justCaptured ? "true" : "false";
      }
      if (els.stageGuideProgress) {
        els.stageGuideProgress.style.strokeDashoffset = String(100 - ringPct);
      }
      const target = Math.max(1, Number(enr.target) || 7);
      if (els.enrollSegments.children.length !== target) {
        els.enrollSegments.textContent = "";
        for (let i = 0; i < target; i++) {
          const seg = document.createElement("span");
          seg.className = "seg";
          els.enrollSegments.appendChild(seg);
        }
        els.enrollSegments.style.setProperty("--seg-count", String(target));
      }
      const segs = els.enrollSegments.querySelectorAll(".seg");
      const segHoldPct = justCaptured ? 0 : holdPct;
      segs.forEach((seg, idx) => {
        seg.classList.toggle("filled", idx < enr.count);
        const current = idx === enr.count;
        seg.classList.toggle("current", current);
        seg.style.setProperty("--hold", current ? `${segHoldPct}%` : "0%");
      });
      setVisible(els.enrollHud, true);
      state.wasEnrolling = true;
    } else {
      if (els.enrollStageGuide) {
        els.enrollStageGuide.dataset.visible = "false";
        els.enrollStageGuide.dataset.captured = "false";
      }
      setVisible(els.enrollHud, false);
      if (state.wasEnrolling) {
        state.wasEnrolling = false;
        const doneUser = live.enroll && live.enroll.completed_user;
        const doneAppend = Boolean(live.enroll && live.enroll.completed_append);
        if (doneUser) {
          const target = Math.max(1, Number((live.enroll && live.enroll.target) || 7));
          const msg = doneAppend
            ? `Appearance added for '${doneUser}'.`
            : `Enrolled ${target} IR samples for '${doneUser}'.`;
          showFeedback(msg, "ok", 5000);
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

    const upd = live.update || {};
    if (isUpdateModalOpen()) {
      renderUpdateProgress(upd);
    }

    if (live.error && !live.error_info) {
      showFeedback(live.error, "error", 6000);
    }

    scheduleNextPoll(active || Boolean(dl.active) || Boolean(upd.active));
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
    renderBitlockerNotice(boot.disk_encryption);
  }

  function applyBootstrap(boot) {
    state.bootstrap = boot;
    state.profiles = boot.profiles || [];

    if (els.btnVersion && boot.version) {
      els.btnVersion.textContent = `v${boot.version}`;
    }

    if (boot.username_locked) {
      // The service only lets each Windows account manage its own profile.
      els.usernameInput.value = boot.active_user || "";
      els.usernameInput.readOnly = true;
      els.usernameInput.removeAttribute("list");
      els.usernameInput.title = "Profiles are tied to the signed-in Windows account";
      els.usernameInput.classList.add("is-locked");
    } else if (!els.usernameInput.value) {
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
    renderBitlockerNotice(boot.disk_encryption);
    renderCameraDropdown(boot.cameras || [], boot.selected_camera ?? 0);
    state.hasIrCamera =
      boot.has_ir_camera !== undefined
        ? Boolean(boot.has_ir_camera)
        : state.cameras.some((c) => c.is_ir === true);
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

  function resetPasswordVisibility() {
    els.pwInput.type = "password";
    els.pwConfirmInput.type = "password";
    els.passwordModal.querySelectorAll(".pw-toggle-btn").forEach((btn) => {
      btn.setAttribute("aria-pressed", "false");
      btn.setAttribute("aria-label", "Show password");
    });
  }

  function isPasswordModalOpen() {
    return els.passwordModal.dataset.open === "true";
  }

  function openPasswordModal() {
    const user = currentUsername();
    if (!user) {
      showFeedback("Enter a Windows username first.", "warn");
      els.usernameInput.focus();
      return;
    }
    closeCameraDropdown(false);
    const prof = findProfile(user);
    els.pwModalUser.textContent = user;
    els.pwDomainInput.value = prof && prof.domain ? prof.domain : ".";
    els.pwInput.value = "";
    els.pwConfirmInput.value = "";
    resetPasswordVisibility();
    els.pwModalError.classList.add("hidden");
    els.passwordModal.dataset.open = "true";
    els.passwordModal.setAttribute("aria-hidden", "false");
    els.pwInput.focus();
  }

  function closePasswordModal() {
    els.pwInput.value = "";
    els.pwConfirmInput.value = "";
    resetPasswordVisibility();
    els.pwModalError.classList.add("hidden");
    els.passwordModal.dataset.open = "false";
    els.passwordModal.setAttribute("aria-hidden", "true");
  }

  function formatAssetSize(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n < 1024 * 1024) {
      return `${Math.max(1, Math.round(n / 1024))} KB`;
    }
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function isUpdateModalOpen() {
    return Boolean(els.updateModal && els.updateModal.dataset.open === "true");
  }

  function renderUpdateNotes(notesText) {
    if (!els.updateNotesBox || !els.updateNotesList) return;
    els.updateNotesList.textContent = "";
    const raw = typeof notesText === "string" ? notesText.trim() : "";
    if (!raw) {
      els.updateNotesBox.classList.add("hidden");
      return;
    }
    const lines = raw.split(/\r?\n/);
    let appended = 0;
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const p = document.createElement("p");
      const isHeading = trimmed.startsWith("#");
      p.className = isHeading ? "update-note-line update-note-heading" : "update-note-line";
      p.textContent = isHeading ? trimmed.replace(/^#{1,6}\s+/, "") : trimmed;
      els.updateNotesList.appendChild(p);
      appended += 1;
    }
    els.updateNotesBox.classList.toggle("hidden", appended === 0);
  }

  function renderUpdateProgress(upd) {
    if (!els.updateProgressRow || !els.btnUpdateInstall) return;
    const stateObj = upd || {};
    const stage = stateObj.stage || "idle";
    const isWorking = Boolean(stateObj.active) || stage === "launching";

    if (isWorking) {
      els.updateProgressRow.classList.remove("hidden");
      els.updateModalError.classList.add("hidden");
      els.btnUpdateInstall.disabled = true;
      els.btnUpdateInstall.textContent = "Installing...";

      const stageLabels = {
        downloading: "Downloading delta package...",
        verifying: "Verifying SHA-256 checksum...",
        extracting: "Extracting package...",
        launching: "Launching elevated updater...",
      };
      els.updateProgressLabel.textContent = stageLabels[stage] || "Preparing update...";

      let pct = 0;
      if (stage === "verifying" || stage === "extracting" || stage === "launching") {
        pct = 100;
      } else if (stateObj.total && stateObj.total > 0) {
        pct = Math.min(100, Math.round((Number(stateObj.downloaded || 0) / Number(stateObj.total)) * 100));
      }
      els.updateProgressPct.textContent = `${pct}%`;
      els.updateProgressFill.style.width = `${pct}%`;
      return;
    }

    els.updateProgressRow.classList.add("hidden");
    els.btnUpdateInstall.disabled = false;
    els.btnUpdateInstall.textContent = "Install Update";

    if (stage === "error" && stateObj.error) {
      els.updateModalError.textContent = stateObj.error;
      els.updateModalError.classList.remove("hidden");
    }
  }

  function applyUpdateBadge(info) {
    state.updateInfo = info;
    if (!els.btnVersion || !info || !info.latest_version) return;
    els.btnVersion.textContent = `Update v${info.latest_version}`;
    els.btnVersion.dataset.state = "update";
    els.btnVersion.title = "View update details";
  }

  function openUpdateModal(info) {
    if (!els.updateModal || !info) return;
    closeCameraDropdown(false);
    const current = info.current_version || (state.bootstrap && state.bootstrap.version) || "0.2.3";
    const latest = info.latest_version || current;
    els.updateVersionLine.textContent = `v${current} -> v${latest}`;

    if (info.can_auto_update) {
      const sizeStr = formatAssetSize(info.update_asset && info.update_asset.size);
      els.updateMetaLine.textContent = sizeStr
        ? `Delta update \u00b7 ${sizeStr} \u00b7 SHA-256 verified`
        : "Delta update \u00b7 SHA-256 verified";
    } else if (info.is_git_checkout) {
      els.updateMetaLine.textContent = "Git checkout detected \u2014 update via git pull";
    } else {
      els.updateMetaLine.textContent = "Full release on GitHub";
    }

    renderUpdateNotes(info.release_notes || "");
    els.btnUpdateInstall.classList.toggle("hidden", !info.can_auto_update);
    els.updateModalError.textContent = "";
    els.updateModalError.classList.add("hidden");
    renderUpdateProgress(state.live && state.live.update);

    els.updateModal.dataset.open = "true";
    els.updateModal.setAttribute("aria-hidden", "false");
  }

  function closeUpdateModal() {
    if (!els.updateModal) return;
    els.updateModal.dataset.open = "false";
    els.updateModal.setAttribute("aria-hidden", "true");
  }

  async function runEnrollAction(user, append) {
    const a = api();
    if (!a) return;
    state.lastAttemptedMode = "enroll";
    state.lastEnrollArgs = { user, append };
    const res = await a.start_enroll(user, append);
    if (res && !res.ok) {
      if (res.error_info || (res.state && res.state.error_info)) {
        if (!res.state || !res.state.error_info) {
          renderStageErrorOverlay(res.error_info);
        }
      } else {
        showFeedback(res.error || "Unable to start enrollment.", "warn");
      }
    }
    if (res && res.state) renderLiveState(res.state);
  }

  async function runLiveTestAction() {
    const a = api();
    if (!a) return;
    state.lastAttemptedMode = "test";
    const res = await a.toggle_live_test();
    if (res && !res.ok) {
      if (res.error_info || (res.state && res.state.error_info)) {
        if (!res.state || !res.state.error_info) {
          renderStageErrorOverlay(res.error_info);
        }
      } else {
        showFeedback(res.error || "Unable to start live test.", "error");
      }
    }
    if (res && res.state) renderLiveState(res.state);
  }

  async function runPreviewAction() {
    const a = api();
    if (!a) return;
    state.lastAttemptedMode = "preview";
    const res = await a.toggle_preview();
    if (res && !res.ok) {
      if (res.error_info || (res.state && res.state.error_info)) {
        if (!res.state || !res.state.error_info) {
          renderStageErrorOverlay(res.error_info);
        }
      } else {
        showFeedback(res.error || "Unable to toggle preview.", "error");
      }
    }
    if (res && res.state) renderLiveState(res.state);
  }

  async function handleRescanCameras() {
    const a = api();
    if (!a || state.isRescanning) return;
    state.isRescanning = true;
    els.btnErrorRescan.disabled = true;
    els.btnErrorRescan.textContent = "Scanning...";
    try {
      const res = await a.rescan_cameras();
      if (res && res.ok) {
        const cams = res.cameras || [];
        const hasIr =
          res.has_ir_camera !== undefined
            ? Boolean(res.has_ir_camera)
            : cams.some((c) => c.is_ir === true);
        state.hasIrCamera = hasIr;
        state.startupNoIrDismissed = false;
        renderCameraDropdown(cams, res.selected_camera ?? 0);
        if (res.state) {
          renderLiveState(res.state);
        }
        if (hasIr) {
          showFeedback("IR camera detected.", "ok", 3500);
        } else {
          renderStageErrorOverlay(NO_IR_ERROR_INFO);
        }
      }
    } finally {
      state.isRescanning = false;
      els.btnErrorRescan.disabled = false;
      els.btnErrorRescan.textContent = "Rescan Cameras";
    }
  }

  async function handleRetryError() {
    const code = state.currentErrorInfo ? state.currentErrorInfo.code : "";
    if (!state.hasIrCamera || code === "no_ir_camera") {
      await handleRescanCameras();
      if (!state.hasIrCamera || !state.lastAttemptedMode) {
        return;
      }
    }
    if (state.lastAttemptedMode === "enroll" && state.lastEnrollArgs) {
      await runEnrollAction(state.lastEnrollArgs.user, state.lastEnrollArgs.append);
    } else if (state.lastAttemptedMode === "test") {
      await runLiveTestAction();
    } else if (state.lastAttemptedMode === "preview") {
      await runPreviewAction();
    } else {
      await handleRescanCameras();
    }
  }

  async function handleDismissError() {
    const a = api();
    state.startupNoIrDismissed = true;
    renderStageErrorOverlay(null);
    if (a && typeof a.clear_error === "function") {
      const res = await a.clear_error();
      if (res && res.state) {
        renderLiveState(res.state);
        return;
      }
    }
    if (state.live) {
      renderLiveState({ ...state.live, error: null, error_info: null });
    }
  }

  function wireEvents() {
    els.streamImg.addEventListener("load", () => {
      revealStreamFrame();
      if (state.live && state.live.camera_active && state.live.mode !== "enroll" && state.live.bbox_norm) {
        updateReticlePosition(state.live.bbox_norm, els.faceReticle.dataset.tone || "neutral");
      }
    });

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
      const prof = findProfile(user);
      const append = Boolean(prof && prof.sample_count > 0);
      await runEnrollAction(user, append);
    });

    els.btnLiveTest.addEventListener("click", runLiveTestAction);
    els.btnPreview.addEventListener("click", runPreviewAction);
    els.btnErrorRetry.addEventListener("click", handleRetryError);
    els.btnErrorRescan.addEventListener("click", handleRescanCameras);
    els.btnErrorDismiss.addEventListener("click", handleDismissError);

    els.btnDiagnostics.addEventListener("click", () => {
      state.diagnosticsVisible = !state.diagnosticsVisible;
      els.btnDiagnostics.setAttribute("aria-pressed", state.diagnosticsVisible ? "true" : "false");
      setDrawerOpen(els.diagnosticsDrawer, state.diagnosticsVisible);
      if (state.live) renderLiveState(state.live);
    });

    els.usernameInput.addEventListener("input", () => {
      setDrawerOpen(els.removeConfirmDrawer, false);
      renderAccountSection();
    });

    els.btnPassword.addEventListener("click", openPasswordModal);
    els.btnPwClose.addEventListener("click", closePasswordModal);
    els.btnPwCancel.addEventListener("click", closePasswordModal);

    els.passwordModal.querySelectorAll(".pw-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = $(btn.dataset.target);
        if (!target) return;
        const revealing = target.type === "password";
        target.type = revealing ? "text" : "password";
        btn.setAttribute("aria-pressed", revealing ? "true" : "false");
        btn.setAttribute("aria-label", revealing ? "Hide password" : "Show password");
      });
    });

    els.passwordModal.addEventListener("click", (ev) => {
      if (ev.target === els.passwordModal) closePasswordModal();
    });

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        if (isUpdateModalOpen()) {
          closeUpdateModal();
          return;
        }
        if (isPasswordModalOpen()) {
          closePasswordModal();
          return;
        }
        if (state.dropdownOpen) {
          ev.preventDefault();
          closeCameraDropdown(true);
        }
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
      setDrawerOpen(els.removeConfirmDrawer, true);
    });

    els.btnRemoveCancel.addEventListener("click", () => {
      setDrawerOpen(els.removeConfirmDrawer, false);
    });

    els.btnRemoveConfirm.addEventListener("click", async () => {
      const a = api();
      if (!a) return;
      const user = currentUsername();
      const res = await a.delete_profile(user);
      setDrawerOpen(els.removeConfirmDrawer, false);
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
      renderScoreStats();
      els.thresholdValue.textContent = parseFloat(els.thresholdSlider.value).toFixed(2);
    });
    els.thresholdSlider.addEventListener("change", persistSecurityConfig);

    if (els.btnBitlockerSettings) {
      els.btnBitlockerSettings.addEventListener("click", async () => {
        const a = api();
        if (!a || typeof a.open_disk_encryption_settings !== "function") return;
        const res = await a.open_disk_encryption_settings();
        if (res && !res.ok && res.error) {
          showFeedback(res.error, "warn");
        }
      });
    }

    const setFeed = async (mode) => {
      const a = api();
      renderFeedMode(mode);
      if (a) await a.set_feed_mode(mode);
    };
    els.btnFeedSubtract.addEventListener("click", () => setFeed("subtract"));
    els.btnFeedBright.addEventListener("click", () => setFeed("bright"));

    /* Custom camera dropdown interactions */
    els.cameraDropdownBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (state.dropdownOpen) {
        closeCameraDropdown(false);
      } else {
        openCameraDropdown();
      }
    });

    els.cameraDropdownBtn.addEventListener("keydown", (ev) => {
      if (!state.dropdownOpen) {
        if (ev.key === "Enter" || ev.key === " " || ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          openCameraDropdown();
        }
        return;
      }

      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        setDropdownFocus(state.dropdownFocusIdx + 1);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        setDropdownFocus(state.dropdownFocusIdx - 1);
      } else if (ev.key === "Home") {
        ev.preventDefault();
        setDropdownFocus(0);
      } else if (ev.key === "End") {
        ev.preventDefault();
        setDropdownFocus(state.cameras.length - 1);
      } else if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        const cam = state.cameras[state.dropdownFocusIdx];
        if (cam) selectCameraOption(Number(cam.index));
      } else if (ev.key === "Tab") {
        closeCameraDropdown(false);
      }
    });

    document.addEventListener("pointerdown", (ev) => {
      if (state.dropdownOpen && els.cameraDropdown && !els.cameraDropdown.contains(ev.target)) {
        closeCameraDropdown(false);
      }
    });

    if (els.settingsPane) {
      els.settingsPane.addEventListener("scroll", () => {
        if (state.dropdownOpen) positionCameraDropdownMenu();
      });
    }

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

    if (els.btnBrandRepo) {
      els.btnBrandRepo.addEventListener("click", async () => {
        const a = api();
        if (!a || typeof a.open_external_url !== "function") return;
        const repoUrl = state.bootstrap && state.bootstrap.repo_url ? state.bootstrap.repo_url : null;
        const res = await a.open_external_url(repoUrl);
        if (res && !res.ok && res.error) {
          showFeedback(res.error, "warn");
        }
      });
    }

    if (els.btnVersion) {
      els.btnVersion.addEventListener("click", async () => {
        const a = api();
        if (!a) return;
        if (state.updateInfo && state.updateInfo.update_available) {
          openUpdateModal(state.updateInfo);
          return;
        }
        if (typeof a.check_for_updates !== "function") return;

        const current = (state.bootstrap && state.bootstrap.version) || "0.2.3";
        els.btnVersion.disabled = true;
        els.btnVersion.textContent = "Checking...";
        try {
          const res = await a.check_for_updates();
          if (res && res.ok && res.update_available) {
            applyUpdateBadge(res);
            openUpdateModal(res);
          } else if (res && res.ok && res.no_releases) {
            els.btnVersion.textContent = `v${current}`;
            showFeedback("No GitHub releases published yet.", "neutral");
          } else if (res && res.ok) {
            els.btnVersion.textContent = `v${current}`;
            showFeedback(`WinIRFaceID v${current} is up to date.`, "neutral");
          } else {
            els.btnVersion.textContent = `v${current}`;
            showFeedback((res && res.error) || "Unable to check for updates.", "warn");
          }
        } catch (_err) {
          els.btnVersion.textContent = `v${current}`;
          showFeedback("Unable to check for updates.", "warn");
        } finally {
          els.btnVersion.disabled = false;
        }
      });
    }

    if (els.btnUpdateClose) {
      els.btnUpdateClose.addEventListener("click", closeUpdateModal);
    }
    if (els.btnUpdateLater) {
      els.btnUpdateLater.addEventListener("click", closeUpdateModal);
    }
    if (els.updateModal) {
      els.updateModal.addEventListener("click", (ev) => {
        if (ev.target === els.updateModal) closeUpdateModal();
      });
    }
    if (els.btnUpdateGithub) {
      els.btnUpdateGithub.addEventListener("click", async () => {
        const a = api();
        if (!a || typeof a.open_external_url !== "function") return;
        const url = (state.updateInfo && state.updateInfo.release_url) || null;
        const res = await a.open_external_url(url);
        if (res && !res.ok && res.error) {
          els.updateModalError.textContent = res.error;
          els.updateModalError.classList.remove("hidden");
        }
      });
    }
    if (els.btnUpdateInstall) {
      els.btnUpdateInstall.addEventListener("click", async () => {
        const a = api();
        if (!a || typeof a.install_update !== "function") return;
        els.updateModalError.classList.add("hidden");
        els.btnUpdateInstall.disabled = true;
        els.btnUpdateInstall.textContent = "Starting...";
        try {
          const res = await a.install_update();
          if (res && res.state) {
            renderLiveState(res.state);
          }
          if (res && res.started) {
            pollLiveState();
          } else if (res && !res.ok) {
            els.updateModalError.textContent = res.error || "Unable to install update.";
            els.updateModalError.classList.remove("hidden");
            els.btnUpdateInstall.disabled = false;
            els.btnUpdateInstall.textContent = "Install Update";
          }
        } catch (_err) {
          els.updateModalError.textContent = "Unable to install update.";
          els.updateModalError.classList.remove("hidden");
          els.btnUpdateInstall.disabled = false;
          els.btnUpdateInstall.textContent = "Install Update";
        }
      });
    }

    window.addEventListener("resize", () => {
      positionSegThumb(false);
      if (state.dropdownOpen) positionCameraDropdownMenu();
      if (state.live && state.live.camera_active && state.live.mode !== "enroll" && state.live.bbox_norm) {
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
    const readyFallbackTimer = setTimeout(markAppReady, 600);
    await waitForPywebview();
    wireEvents();
    const a = api();
    const boot = await a.get_bootstrap();
    applyBootstrap(boot);
    const live = await a.get_state();
    renderLiveState(live);

    clearTimeout(readyFallbackTimer);
    requestAnimationFrame(() => {
      positionSegThumb(false);
      markAppReady();
    });

    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => positionSegThumb(false)).catch(() => {});
    }

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

    // Silent, non-blocking startup update check (never shows errors when offline).
    setTimeout(async () => {
      const currApi = api();
      if (!currApi || typeof currApi.check_for_updates !== "function") return;
      try {
        const res = await currApi.check_for_updates();
        if (res && res.ok && res.update_available) {
          applyUpdateBadge(res);
        }
      } catch (_err) {
        // Ignore silent startup check failures
      }
    }, 1200);
  }

  init().catch((err) => {
    markAppReady();
    showFeedback(`Initialization error: ${err}`, "error", 0);
  });
})();
