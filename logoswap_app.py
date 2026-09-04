"""
logoswap_app.py  –  Single-file launcher for the logoswap web UI.

SHARE THIS FILE (+ the logoswap/ folder) with your team.

HOW TO RUN
----------
  1. Install dependencies (once):
       pip install flask numpy Pillow opencv-python

  2. Make sure ffmpeg & ffprobe are on your PATH:
       https://ffmpeg.org/download.html

  3. Start the app:
       python logoswap_app.py

  The browser opens automatically at http://localhost:5000
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

from flask import Blueprint, Flask, jsonify, request

# ── Embedded HTML ─────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>logoswap</title>
<link rel="icon" type="image/png" href="__LOGO_URI__">
<link rel="apple-touch-icon" href="__LOGO_URI__">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:       #0d1117;
  --surf:     #161b22;
  --surf2:    #21262d;
  --border:   #30363d;
  --accent:   #7c6fe0;
  --acc-h:    #9b8fec;
  --acc-glow: rgba(124,111,224,.35);
  --ok:       #3fb950;
  --ok-bg:    rgba(63,185,80,.12);
  --err:      #f85149;
  --err-bg:   rgba(248,81,73,.12);
  --warn:     #e3b341;
  --text:     #e6edf3;
  --text2:    #8b949e;
  --text3:    #6e7681;
  --r:        12px;
  --r-sm:     8px;
  --r-xs:     5px;
  --trans:    .2s ease;
}
html { font-size: 14px; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
  line-height: 1.5;
  min-height: 100vh;
  padding-bottom: 80px;
}
body::before {
  content: '';
  position: fixed; inset: 0; pointer-events: none;
  background: radial-gradient(ellipse 70% 40% at 50% -5%,
    rgba(124,111,224,.12) 0%, transparent 70%);
}

/* ── Nav ── */
nav {
  display: flex; align-items: center; gap: 14px;
  padding: 18px 32px;
  border-bottom: 1px solid var(--border);
  background: rgba(22,27,34,.85);
  backdrop-filter: blur(14px);
  position: sticky; top: 0; z-index: 100;
}
.nav-logo { line-height: 0; display: flex; align-items: center; }
.nav-logo img { width: 30px; height: 30px; display: block; }
.nav-title {
  font-size: 21px; font-weight: 800;
  background: linear-gradient(135deg, var(--text) 0%, var(--acc-h) 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.nav-sub { font-size: 12px; color: var(--text3); margin-top: 1px; }
.nav-pill {
  margin-left: auto;
  font-size: 11px; color: var(--text3);
  background: var(--surf2); border: 1px solid var(--border);
  padding: 3px 11px; border-radius: 20px; letter-spacing: .02em;
}

/* ── Layout ── */
main { max-width: 1020px; margin: 0 auto; padding: 36px 24px; }
section-title, h2 {
  font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .08em; color: var(--text3);
  margin-bottom: 14px; display: block;
}

/* ── Cards ── */
.card {
  background: var(--surf);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 22px;
}
.card-label {
  font-size: 12px; font-weight: 600; color: var(--text2);
  margin-bottom: 14px;
  display: flex; align-items: center; gap: 8px;
}

/* ── Upload row ── */
.upload-row {
  display: grid; grid-template-columns: 1fr 290px; gap: 16px;
  margin-bottom: 16px;
}
.drop-zone {
  border: 2px dashed var(--border);
  border-radius: var(--r-sm);
  padding: 30px 20px;
  text-align: center;
  cursor: pointer;
  transition: all var(--trans);
  position: relative;
}
.drop-zone:hover, .drop-zone.over {
  border-color: var(--accent);
  background: rgba(124,111,224,.07);
}
.drop-zone.has-files { border-color: var(--ok); border-style: solid; }
.drop-zone input[type=file] {
  position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
}
.dz-icon  { font-size: 34px; margin-bottom: 10px; }
.dz-label { font-size: 14px; color: var(--text2); }
.dz-hint  { font-size: 11px; color: var(--text3); margin-top: 5px; }

/* File chips */
.file-list { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.chip {
  display: flex; align-items: center; gap: 8px;
  background: var(--surf2); border: 1px solid var(--border);
  border-radius: var(--r-xs); padding: 7px 11px;
  font-size: 12px; transition: border-color var(--trans);
}
.chip:hover { border-color: var(--accent); }
.chip-ok   { color: var(--ok); }
.chip-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip-size { color: var(--text3); flex-shrink: 0; }
.chip-del  {
  color: var(--text3); cursor: pointer; background: none; border: none;
  font-size: 15px; line-height: 1; transition: color var(--trans); flex-shrink: 0;
}
.chip-del:hover { color: var(--err); }

/* Logo zone */
.logo-zone .drop-zone { min-height: 130px; }
.logo-preview-wrap { margin-top: 14px; display: flex; justify-content: center; }
.logo-preview {
  width: 96px; height: 96px;
  border-radius: var(--r-sm); object-fit: contain;
  border: 1px solid var(--border); background: var(--surf2);
}
.logo-name {
  margin-top: 8px; text-align: center;
  font-size: 11px; color: var(--text3);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* ── Settings ── */
#settings-card { margin-bottom: 16px; }
.settings-toggle {
  display: flex; align-items: center; justify-content: space-between;
  cursor: pointer; user-select: none;
  background: none; border: none; width: 100%;
  color: var(--text2); font-size: 13px; font-weight: 600; padding: 0;
}
.settings-toggle:hover { color: var(--text); }
.toggle-arrow { font-size: 11px; transition: transform .25s; }
.toggle-arrow.open { transform: rotate(180deg); }
#settings-body { overflow: hidden; max-height: 0; transition: max-height .35s ease, opacity .25s; opacity: 0; }
#settings-body.open { max-height: 620px; opacity: 1; }
.settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 22px; padding-top: 18px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 12px; color: var(--text2); font-weight: 500; }
.field input[type=text], .field input[type=number] {
  background: var(--surf2); border: 1px solid var(--border);
  border-radius: var(--r-xs); padding: 8px 11px;
  color: var(--text); font-size: 13px;
  outline: none; transition: border-color var(--trans);
}
.field input:focus { border-color: var(--accent); }
.field input::placeholder { color: var(--text3); }
.range-wrap { display: flex; align-items: center; gap: 10px; }
.range-wrap input[type=range] { flex: 1; accent-color: var(--accent); }
.range-val { min-width: 38px; text-align: right; font-size: 13px; font-weight: 700; color: var(--accent); }
.checkbox-row { display: flex; gap: 18px; flex-wrap: wrap; }
.checkbox-label {
  display: flex; align-items: center; gap: 7px;
  cursor: pointer; font-size: 13px; color: var(--text2); user-select: none;
}
.checkbox-label input { accent-color: var(--accent); width: 14px; height: 14px; }
.field-hint { font-size: 11px; color: var(--text3); }
.full-col { grid-column: 1 / -1; }

/* ── Run ── */
.run-row { display: flex; justify-content: center; margin: 22px 0 30px; }
#run-btn {
  background: linear-gradient(135deg, var(--accent) 0%, #8b5cf6 100%);
  color: #fff; border: none; border-radius: var(--r);
  padding: 15px 58px; font-size: 16px; font-weight: 800;
  cursor: pointer;
  box-shadow: 0 4px 24px var(--acc-glow);
  transition: all var(--trans);
  display: flex; align-items: center; gap: 11px;
}
#run-btn:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 8px 32px var(--acc-glow); }
#run-btn:active:not(:disabled) { transform: translateY(0); }
#run-btn:disabled { opacity: .4; cursor: not-allowed; transform: none; }
.btn-spinner {
  width: 17px; height: 17px; border: 2px solid rgba(255,255,255,.35);
  border-top-color: #fff; border-radius: 50%;
  animation: spin .7s linear infinite; display: none;
}
#run-btn.loading .btn-spinner { display: block; }
#run-btn.loading .btn-text { opacity: .75; }

/* ── Progress ── */
.video-grid { display: flex; flex-direction: column; gap: 11px; margin-bottom: 18px; }
.vcard {
  background: var(--surf); border: 1px solid var(--border);
  border-radius: var(--r); padding: 17px 20px;
  transition: border-color var(--trans);
}
.vcard.done  { border-color: var(--ok); }
.vcard.error { border-color: var(--err); }
.vcard-head { display: flex; align-items: center; gap: 11px; margin-bottom: 11px; }
.vcard-icon { font-size: 18px; flex-shrink: 0; }
.vcard-name { font-size: 13px; font-weight: 600; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vcard-status {
  font-size: 11px; font-weight: 700; padding: 3px 10px;
  border-radius: 20px; flex-shrink: 0;
}
.status-queued  { background: var(--surf2); color: var(--text3); }
.status-running { background: rgba(124,111,224,.2); color: var(--acc-h); }
.status-done    { background: var(--ok-bg); color: var(--ok); }
.status-error   { background: var(--err-bg); color: var(--err); }
.bar-track { background: var(--surf2); border-radius: 4px; height: 5px; overflow: hidden; }
.bar-fill {
  height: 100%; border-radius: 4px; width: 0%;
  background: linear-gradient(90deg, var(--accent), var(--acc-h));
  transition: width .8s cubic-bezier(.4,0,.2,1);
}
.bar-fill.shimmer {
  background-image: linear-gradient(90deg,
    var(--accent) 0%, var(--acc-h) 40%, #c4b5fd 60%, var(--acc-h) 80%, var(--accent) 100%);
  background-size: 300% 100%;
  animation: shimmer 1.8s ease-in-out infinite;
}
.bar-fill.complete { background: var(--ok); }
.bar-fill.failed   { background: var(--err); }
.vcard-stage { font-size: 11px; color: var(--text3); margin-top: 7px; }

/* Log */
.log-card { background: #0a0c10; border: 1px solid var(--border); border-radius: var(--r); overflow: hidden; }
.log-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 16px; background: var(--surf2); border-bottom: 1px solid var(--border);
}
.log-header-title { font-size: 12px; font-weight: 600; color: var(--text3); }
.log-clear { font-size: 11px; color: var(--text3); cursor: pointer; background: none; border: none; transition: color var(--trans); }
.log-clear:hover { color: var(--text); }
#log-body {
  height: 230px; overflow-y: auto; padding: 11px 16px;
  font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace;
  font-size: 11.5px; line-height: 1.68;
}
#log-body::-webkit-scrollbar { width: 5px; }
#log-body::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.log-line { white-space: pre-wrap; word-break: break-all; }
.log-INFO    { color: #8b949e; }
.log-WARNING { color: var(--warn); }
.log-ERROR   { color: var(--err); }

/* ── Results ── */
.results-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 14px; }
.result-card {
  background: var(--surf); border: 1px solid var(--ok);
  border-radius: var(--r); padding: 18px;
  display: flex; flex-direction: column; gap: 13px;
  transition: transform var(--trans), box-shadow var(--trans);
}
.result-card:hover { transform: translateY(-2px); box-shadow: 0 6px 22px rgba(63,185,80,.15); }
.result-card.preview-card { border-color: var(--accent); }
.result-card.preview-card:hover { box-shadow: 0 6px 22px var(--acc-glow); }
.result-card.error-card { border-color: var(--err); opacity: .7; }
.result-icon { font-size: 30px; text-align: center; }
.result-name { font-size: 13px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-sub  { font-size: 11px; color: var(--text3); margin-top: 3px; }
.result-img  { width: 100%; border-radius: var(--r-xs); border: 1px solid var(--border); max-height: 130px; object-fit: contain; background: var(--surf2); }
.btn-dl {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  background: var(--ok); color: #000;
  border: none; border-radius: var(--r-sm);
  padding: 10px 18px; font-size: 12px; font-weight: 800;
  cursor: pointer; text-decoration: none;
  transition: all var(--trans);
}
.btn-dl:hover { background: #5bc96a; transform: translateY(-1px); }
.btn-view  { background: var(--accent); color: #fff; }
.btn-view:hover { background: var(--acc-h); }
.btn-cs    { background: var(--surf2); color: var(--text2); border: 1px solid var(--border); font-size: 11px; padding: 7px 14px; }
.btn-cs:hover { border-color: var(--accent); color: var(--acc-h); }
.btn-error { background: var(--err-bg); color: var(--err); border: 1px solid var(--err); cursor: default; }
.divider { border: none; border-top: 1px solid var(--border); margin: 26px 0; }

/* ── Empty states ── */
.empty-state { text-align: center; padding: 52px 24px; color: var(--text3); font-size: 13px; }
.empty-state .big-icon { font-size: 44px; margin-bottom: 14px; }

/* ── Animations ── */
@keyframes spin    { to { transform: rotate(360deg); } }
@keyframes shimmer { 0%,100% { background-position: 100% 0; } 50% { background-position: 0% 0; } }
@keyframes fadein  { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
.fadein { animation: fadein .3s ease forwards; }

/* ── Responsive ── */
@media (max-width: 660px) {
  .upload-row { grid-template-columns: 1fr; }
  .settings-grid { grid-template-columns: 1fr; }
  nav { padding: 14px 16px; }
  main { padding: 24px 14px; }
}
</style>
</head>
<body>

<nav>
  <span class="nav-logo"><img src="__LOGO_URI__" alt="logoswap"></span>
  <div>
    <div class="nav-title">logoswap</div>
    <div class="nav-sub">Automated game icon replacement</div>
  </div>
  <div class="nav-pill">v1.0</div>
</nav>

<main>

  <!-- Upload row -->
  <div class="upload-row">
    <div class="card">
      <div class="card-label">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
          <path d="M0 3.5A1.5 1.5 0 0 1 1.5 2h11A1.5 1.5 0 0 1 14 3.5v9A1.5 1.5 0 0 1 12.5 14h-11A1.5 1.5 0 0 1 0 12.5v-9ZM1.5 3a.5.5 0 0 0-.5.5v9a.5.5 0 0 0 .5.5h11a.5.5 0 0 0 .5-.5v-9a.5.5 0 0 0-.5-.5h-11ZM11 6l-3.5 2L11 10V6Z"/>
        </svg>
        Videos
      </div>
      <div class="drop-zone" id="videos-zone">
        <input type="file" id="videos-input" multiple accept=".mp4,.mov,.avi,.mkv,.webm">
        <div class="dz-icon">🎞️</div>
        <div class="dz-label">Drop videos here</div>
        <div class="dz-hint">or click to browse &nbsp;·&nbsp; mp4 mov avi mkv webm</div>
      </div>
      <div class="file-list" id="video-chips"></div>
    </div>

    <div class="card logo-zone">
      <div class="card-label">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
          <path d="M6.002 5.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0Z"/>
          <path d="M2.002 1a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V3a2 2 0 0 0-2-2h-12Zm12 1a1 1 0 0 1 1 1v6.5l-3.777-1.947a.5.5 0 0 0-.577.093l-3.71 3.71-2.66-1.772a.5.5 0 0 0-.63.062L1.002 12V3a1 1 0 0 1 1-1h12Z"/>
        </svg>
        New Logo (PNG)
      </div>
      <div class="drop-zone" id="logo-zone">
        <input type="file" id="logo-input" accept=".png,.jpg,.jpeg,.webp">
        <div class="dz-icon">🖼️</div>
        <div class="dz-label">Drop logo here</div>
        <div class="dz-hint">PNG recommended &nbsp;·&nbsp; transparent bg ideal</div>
      </div>
      <div id="logo-preview-area"></div>
    </div>
  </div>

  <!-- Settings -->
  <div class="card" id="settings-card" style="margin-bottom:16px">
    <button class="settings-toggle" onclick="toggleSettings()" type="button">
      <span>⚙️ &nbsp;Advanced settings</span>
      <span class="toggle-arrow" id="toggle-arrow">▾</span>
    </button>
    <div id="settings-body">
      <div class="settings-grid">

        <div class="field">
          <label>Size margin (coverage over old icon)</label>
          <div class="range-wrap">
            <input type="range" id="margin-range" min="0" max="0.25" step="0.01" value="0.16"
              oninput="document.getElementById('margin-val').textContent=(this.value*100).toFixed(0)+'%'">
            <span class="range-val" id="margin-val">16%</span>
          </div>
          <span class="field-hint">How much larger the replacement logo is vs the detected icon</span>
        </div>

        <div class="field">
          <label>Region override <span style="color:var(--text3)">(X Y W H)</span></label>
          <input type="text" id="region" placeholder="e.g. 537 307 326 326" autocomplete="off">
          <span class="field-hint">Skips auto-detection. Enable Preview Mode first to measure.</span>
        </div>

        <div class="field">
          <label>Pop onset time (TS) in seconds</label>
          <input type="number" id="start" placeholder="auto-tracked" step="0.01" min="0">
        </div>
        <div class="field">
          <label>Settle time (TSET) in seconds</label>
          <input type="number" id="settle" placeholder="auto-tracked" step="0.01" min="0">
        </div>

        <div class="field">
          <label>End-card scan window (seconds from end)</label>
          <input type="number" id="end-window" value="8" min="2" max="30" step="1">
        </div>
        <div class="field">
          <label>Pop tracking window (seconds after cut)</label>
          <input type="number" id="track-window" value="2.5" min="0.5" max="10" step="0.5">
        </div>

        <div class="field full-col">
          <label>Options</label>
          <div class="checkbox-row">
            <label class="checkbox-label">
              <input type="checkbox" id="preview-mode">
              Preview only (detect PNG, no render)
            </label>
            <label class="checkbox-label">
              <input type="checkbox" id="contact-sheet">
              Contact sheet (QA strip per video)
            </label>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Run button -->
  <div class="run-row">
    <button id="run-btn" onclick="runJob()" disabled>
      <div class="btn-spinner"></div>
      <span class="btn-text">▶ &nbsp;Run logoswap</span>
    </button>
  </div>

  <!-- Progress -->
  <div id="progress-section" style="display:none">
    <h2>Progress</h2>
    <div class="video-grid" id="video-grid"></div>
    <div class="log-card">
      <div class="log-header">
        <span class="log-header-title">📋 Log output</span>
        <button class="log-clear" onclick="clearLog()">clear</button>
      </div>
      <div id="log-body"></div>
    </div>
    <hr class="divider">
  </div>

  <!-- Results -->
  <div id="results-section" style="display:none">
    <h2>Results</h2>
    <div class="results-grid" id="results-grid"></div>
  </div>

</main>

<script>
'use strict';
const S = { videos: [], logo: null, jobId: null, cards: {} };

function apiUrl(path) {
  const base = (typeof window !== 'undefined' && window.__APP_BASE_PATH__) || '';
  return base + path;
}

function getOptions() {
  return {
    margin:        parseFloat(document.getElementById('margin-range').value),
    region:        document.getElementById('region').value.trim(),
    start:         document.getElementById('start').value.trim(),
    settle:        document.getElementById('settle').value.trim(),
    end_window:    parseFloat(document.getElementById('end-window').value),
    track_window:  parseFloat(document.getElementById('track-window').value),
    preview:       document.getElementById('preview-mode').checked,
    contact_sheet: document.getElementById('contact-sheet').checked,
  };
}

function toggleSettings() {
  const body = document.getElementById('settings-body');
  const arrow = document.getElementById('toggle-arrow');
  const open = body.classList.toggle('open');
  arrow.classList.toggle('open', open);
}

function refreshRunBtn() {
  document.getElementById('run-btn').disabled = !(S.videos.length > 0 && S.logo !== null);
}

function setupDropZone(zoneId, inputId, multiple, onFiles) {
  const zone  = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  zone.addEventListener('dragover',  e => { e.preventDefault(); zone.classList.add('over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('over'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('over');
    const files = multiple ? Array.from(e.dataTransfer.files) : [e.dataTransfer.files[0]].filter(Boolean);
    if (files.length) onFiles(files);
  });
  input.addEventListener('change', () => {
    const files = multiple ? Array.from(input.files) : [input.files[0]].filter(Boolean);
    if (files.length) onFiles(files);
    input.value = '';
  });
}

async function uploadFiles(files) {
  const fd = new FormData();
  files.forEach(f => fd.append('files', f));
  const res = await fetch(apiUrl('/upload'), { method: 'POST', body: fd });
  if (!res.ok) throw new Error('Upload failed: ' + res.status);
  return res.json();
}

async function handleVideoFiles(files) {
  const zone  = document.getElementById('videos-zone');
  const chips = document.getElementById('video-chips');
  for (const file of files) {
    if (S.videos.find(v => v.name === file.name)) continue;
    try {
      const [entry] = await uploadFiles([file]);
      S.videos.push(entry);
      chips.appendChild(makeChip(entry, () => removeVideo(entry.id)));
      zone.classList.add('has-files');
    } catch(e) { console.error(e); }
  }
  refreshRunBtn();
}

function removeVideo(id) {
  S.videos = S.videos.filter(v => v.id !== id);
  renderVideoChips();
  if (!S.videos.length) document.getElementById('videos-zone').classList.remove('has-files');
  refreshRunBtn();
}

function renderVideoChips() {
  const chips = document.getElementById('video-chips');
  chips.innerHTML = '';
  S.videos.forEach(v => chips.appendChild(makeChip(v, () => removeVideo(v.id))));
}

function makeChip(entry, onDel) {
  const div = document.createElement('div');
  div.className = 'chip';
  div.innerHTML = `<span class="chip-ok">✓</span><span class="chip-name" title="${esc(entry.name)}">${esc(entry.name)}</span><button class="chip-del" title="Remove">✕</button>`;
  div.querySelector('.chip-del').addEventListener('click', onDel);
  return div;
}

async function handleLogoFile(files) {
  const file = files[0]; if (!file) return;
  const zone = document.getElementById('logo-zone');
  try {
    const [entry] = await uploadFiles([file]);
    S.logo = entry;
    zone.classList.add('has-files');
    const area = document.getElementById('logo-preview-area');
    const reader = new FileReader();
    reader.onload = e => {
      area.innerHTML = `<div class="logo-preview-wrap"><img class="logo-preview" src="${e.target.result}" alt="logo"></div><div class="logo-name">${esc(file.name)}</div>`;
    };
    reader.readAsDataURL(file);
    refreshRunBtn();
  } catch(e) { console.error(e); }
}

async function runJob() {
  if (!S.logo || !S.videos.length) return;
  S.cards = {};
  ['video-grid','log-body','results-grid'].forEach(id => { document.getElementById(id).innerHTML = ''; });
  document.getElementById('progress-section').style.display = '';
  document.getElementById('results-section').style.display = 'none';
  window.scrollTo({ top: document.getElementById('progress-section').offsetTop - 80, behavior: 'smooth' });

  const btn = document.getElementById('run-btn');
  btn.disabled = true; btn.classList.add('loading');
  btn.querySelector('.btn-text').textContent = '⏳  Running…';

  S.videos.forEach((v, i) => addVideoCard(v.name, i));

  try {
    const res = await fetch(apiUrl('/run'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ logo_id: S.logo.id, video_ids: S.videos.map(v => v.id), options: getOptions() }),
    });
    const data = await res.json();
    if (!res.ok) { alert('Error: ' + (data.error || res.status)); resetRunBtn(); return; }
    S.jobId = data.job_id;
    pollJob(data.job_id);
  } catch(e) { alert('Network error: ' + e.message); resetRunBtn(); }
}

function resetRunBtn() {
  const btn = document.getElementById('run-btn');
  btn.disabled = false; btn.classList.remove('loading');
  btn.querySelector('.btn-text').textContent = '▶ \u00a0Run logoswap';
}

function pollJob(jobId) {
  let cursor = 0;
  let finished = false;
  const tick = async () => {
    if (finished) return;
    try {
      const res = await fetch(apiUrl('/poll/' + jobId + '?since=' + cursor));
      if (res.ok) {
        const data = await res.json();
        cursor = data.next;
        (data.events || []).forEach(handleMsg);
        if (data.status === 'done') {
          finished = true;
          resetRunBtn();
          showResults(data.results);
          return;
        }
      }
    } catch (_) { /* transient network error — keep polling */ }
    setTimeout(tick, 800);
  };
  tick();
}

function handleMsg(msg) {
  if (msg.type === 'video_start') {
    setVideoStatus(msg.video, 'running', '');
    setVideoProgress(msg.video, 5, 'Starting…');
  } else if (msg.type === 'log') {
    appendLog(msg);
    const p = extractProgress(msg.message);
    if (p) setVideoProgress(msg.video, p.pct, p.stage);
  } else if (msg.type === 'video_done') {
    setVideoStatus(msg.video, 'done', '');
    setVideoProgress(msg.video, 100, msg.is_preview ? 'Preview saved' : 'Complete!');
  } else if (msg.type === 'video_error') {
    setVideoStatus(msg.video, 'error', msg.error || '');
    setVideoProgress(msg.video, 100, 'Failed');
  } else if (msg.type === 'job_done') {
    resetRunBtn();
    showResults(msg.results);
  }
}

const STAGES = [
  { re: /\d+x\d+.*fps/i,               pct: 10,  stage: 'Probed' },
  { re: /Extracting settled/,           pct: 18,  stage: 'Analysing end-card…' },
  { re: /Icon onset/,                   pct: 32,  stage: 'End-card found' },
  { re: /Icon:.*center/,                pct: 46,  stage: 'Icon detected' },
  { re: /Low detection confidence/,     pct: 46,  stage: 'Icon (low conf)' },
  { re: /Slide-from-top/,               pct: 54,  stage: 'Checking animation type…' },
  { re: /Pop tracking (OK|FALLBACK)/,   pct: 66,  stage: 'Animation tracked' },
  { re: /Logo:.*px/,                    pct: 71,  stage: 'Logo prepared' },
  { re: /Rendering \(animated/,         pct: 78,  stage: 'Rendering…' },
  { re: /Rendering \(static/,           pct: 78,  stage: 'Rendering…' },
  { re: /Encoding .*preset/,            pct: 85,  stage: 'Encoding video…' },
  { re: /Encoded .* in /,               pct: 96,  stage: 'Encoded' },
  { re: /Done →/,                       pct: 100, stage: 'Complete!' },
  { re: /FAILED/,                       pct: 100, stage: 'Failed' },
];
function extractProgress(msg) {
  for (const s of STAGES) if (s.re.test(msg)) return { pct: s.pct, stage: s.stage };
  return null;
}

function addVideoCard(name) {
  const grid = document.getElementById('video-grid');
  const card = document.createElement('div');
  const cid = cardId(name);
  card.className = 'vcard fadein'; card.id = 'vcard-' + cid;
  card.innerHTML = `
    <div class="vcard-head">
      <span class="vcard-icon">🎬</span>
      <span class="vcard-name" title="${esc(name)}">${esc(name)}</span>
      <span class="vcard-status status-queued" id="vstatus-${cid}">Queued</span>
    </div>
    <div class="bar-track"><div class="bar-fill" id="vbar-${cid}"></div></div>
    <div class="vcard-stage" id="vstage-${cid}">Waiting…</div>
  `;
  grid.appendChild(card);
  S.cards[name] = {
    bar:    document.getElementById('vbar-'    + cid),
    stage:  document.getElementById('vstage-' + cid),
    status: document.getElementById('vstatus-'+ cid),
    card,
  };
}

function setVideoProgress(name, pct, stage) {
  const c = S.cards[name]; if (!c) return;
  c.bar.style.width = pct + '%';
  if (stage) c.stage.textContent = stage;
  if (pct > 0 && pct < 100) { c.bar.classList.add('shimmer'); c.bar.classList.remove('complete','failed'); }
  else if (pct >= 100) c.bar.classList.remove('shimmer');
}

function setVideoStatus(name, status, extra) {
  const c = S.cards[name]; if (!c) return;
  const labels = { running:'Running', done:'Done', error:'Error', queued:'Queued' };
  c.status.className = 'vcard-status status-' + status;
  c.status.textContent = labels[status] || status;
  c.card.classList.toggle('done',  status === 'done');
  c.card.classList.toggle('error', status === 'error');
  if (status === 'done')  { c.bar.classList.remove('shimmer'); c.bar.classList.add('complete'); c.card.querySelector('.vcard-icon').textContent = '✅'; }
  if (status === 'error') { c.bar.classList.remove('shimmer'); c.bar.classList.add('failed');   c.card.querySelector('.vcard-icon').textContent = '❌'; if (extra) c.stage.textContent = extra; }
}

function appendLog(msg) {
  const body = document.getElementById('log-body');
  const line = document.createElement('div');
  line.className = 'log-line log-' + (msg.level || 'INFO');
  line.textContent = msg.message || '';
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
}
function clearLog() { document.getElementById('log-body').innerHTML = ''; }

function showResults(results) {
  const sec  = document.getElementById('results-section');
  const grid = document.getElementById('results-grid');
  sec.style.display = '';
  grid.innerHTML = '';
  if (!results || !results.length) {
    grid.innerHTML = '<div class="empty-state"><div class="big-icon">😶</div>No results.</div>';
    return;
  }
  results.forEach(r => {
    const card = document.createElement('div');
    card.className = 'result-card fadein';
    if (!r.ok) {
      card.classList.add('error-card');
      card.innerHTML = `<div class="result-icon">❌</div><div class="result-info"><div class="result-name">${esc(r.video)}</div><div class="result-sub">${esc(r.error||'Failed')}</div></div><button class="btn-dl btn-error" disabled>Failed</button>`;
    } else if (r.is_preview) {
      card.classList.add('preview-card');
      card.innerHTML = `<div class="result-icon">🔍</div><div class="result-info"><div class="result-name">${esc(r.video)}</div><div class="result-sub">Detection preview</div></div><img class="result-img" src="${apiUrl('/download/' + S.jobId + '/' + r.output)}" alt="preview"><a class="btn-dl btn-view" href="${apiUrl('/download/' + S.jobId + '/' + r.output)}" download="${esc(r.output)}">⬇ Download preview</a>`;
    } else {
      const cs = r.contact_sheet ? `<a class="btn-dl btn-cs" href="${apiUrl('/download/' + S.jobId + '/' + r.contact_sheet)}" download="${esc(r.contact_sheet)}">📋 Contact sheet</a>` : '';
      card.innerHTML = `<div class="result-icon">🎉</div><div class="result-info"><div class="result-name">${esc(r.output)}</div><div class="result-sub">Rendered successfully</div></div><a class="btn-dl" href="${apiUrl('/download/' + S.jobId + '/' + r.output)}" download="${esc(r.output)}">⬇ Download video</a>${cs}`;
    }
    grid.appendChild(card);
  });
  sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function cardId(n) { return n.replace(/[^a-z0-9]/gi,'_'); }

setupDropZone('videos-zone', 'videos-input', true,  handleVideoFiles);
setupDropZone('logo-zone',   'logo-input',   false, handleLogoFile);
</script>
</body>
</html>"""

# ── Flask app ─────────────────────────────────────────────────────────────────

_BASE_PATH = os.environ.get("APP_BASE_PATH", "").rstrip("/")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 600 * 1024 * 1024

WORK_DIR = Path(tempfile.mkdtemp(prefix="logoswap_ui_"))
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
bp = Blueprint("logoswap", __name__)


# Tool logo (blue swap-arrows), embedded as a PNG data URI so the app
# stays a single self-contained file.
_LOGO_DATA_URI = "data:image/png;base64,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"


def _index_html() -> str:
    html = _HTML.replace(
        "<head>",
        f"<head><script>window.__APP_BASE_PATH__={json.dumps(_BASE_PATH)};</script>",
        1,
    )
    return html.replace("__LOGO_URI__", _LOGO_DATA_URI)


def _emit(job_id: str, event: dict) -> None:
    """Append a progress/log event to the job's event log (read via /poll)."""
    job = _jobs.get(job_id)
    if job is not None:
        job["events"].append(event)


class _ListHandler(logging.Handler):
    """Logging handler that appends log records to a job's event list."""

    def __init__(self, job_id: str) -> None:
        super().__init__()
        self.job_id = job_id
        self.video_name = ""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _emit(self.job_id, {
                "type": "log",
                "video": self.video_name,
                "level": record.levelname,
                "message": self.format(record),
            })
        except Exception:
            pass


@bp.route("/")
def index():
    return _index_html(), 200, {"Content-Type": "text/html; charset=utf-8"}


@bp.route("/upload", methods=["POST"])
def upload():
    saved = []
    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        fid = str(uuid.uuid4())
        dest = WORK_DIR / fid
        dest.mkdir(parents=True)
        fpath = dest / f.filename
        f.save(str(fpath))
        saved.append({"name": f.filename, "id": fid})
    return jsonify(saved)


@bp.route("/run", methods=["POST"])
def start_job():
    data = request.get_json(force=True)

    logo = _find_upload(data.get("logo_id", ""))
    if not logo:
        return jsonify({"error": "Logo file not found on server"}), 400

    videos = [v for vid in data.get("video_ids", []) if (v := _find_upload(vid))]
    if not videos:
        return jsonify({"error": "No video files found on server"}), 400

    job_id = str(uuid.uuid4())
    output_dir = WORK_DIR / job_id / "output"
    output_dir.mkdir(parents=True)

    with _lock:
        _jobs[job_id] = {"status": "running", "events": [], "results": [], "output_dir": str(output_dir)}

    threading.Thread(
        target=_run_job,
        args=(job_id, logo, videos, output_dir, data.get("options", {})),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@bp.route("/poll/<job_id>")
def poll(job_id: str):
    """Return job events accumulated since ?since=<n>, plus current status.

    Uses plain HTTP polling instead of SSE so it works reliably behind
    proxies / Kubernetes ingress (CAP) that buffer streaming responses.
    """
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404

    since = request.args.get("since", default=0, type=int)
    events = job["events"][since:]
    return jsonify({
        "events": events,
        "next": since + len(events),
        "status": job["status"],
        "results": job["results"] if job["status"] == "done" else [],
    })


@bp.route("/download/<job_id>/<filename>")
def download(job_id: str, filename: str):
    if job_id not in _jobs:
        return "Job not found", 404
    path = Path(_jobs[job_id]["output_dir"]) / filename
    if not path.is_file():
        return "File not found", 404
    as_attach = not filename.lower().endswith(".png")
    from flask import send_file
    return send_file(str(path), as_attachment=as_attach)


def _run_job(job_id, logo, videos, output_dir, options):
    import argparse
    from logoswap.__main__ import _process_one

    ns = argparse.Namespace(
        margin=float(options.get("margin", 0.16)),
        region=_parse_region(options.get("region", "")),
        start=_to_float(options.get("start")),
        settle=_to_float(options.get("settle")),
        preview=bool(options.get("preview", False)),
        contact_sheet=bool(options.get("contact_sheet", False)),
        keep_temp=False,
        end_window=float(options.get("end_window", 8.0)),
        track_window=float(options.get("track_window", 2.5)),
        verbose=False,
    )

    handler = _ListHandler(job_id)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    ls_log = logging.getLogger("logoswap")
    # The web app never runs the CLI's logging.basicConfig(), so the logoswap
    # logger would default to the root level (WARNING) under gunicorn and drop
    # all INFO progress logs. Force INFO here so live logs reach the UI.
    ls_log.setLevel(logging.INFO)
    ls_log.addHandler(handler)

    logo_path = Path(logo["path"])
    results: list[dict] = []

    try:
        for i, entry in enumerate(videos):
            vname = entry["name"]
            vpath = Path(entry["path"])
            handler.video_name = vname

            _emit(job_id, {"type": "video_start", "video": vname, "index": i, "total": len(videos)})

            try:
                out = _process_one(vpath, logo_path, output_dir, ns, logo_path.stem)
            except Exception as exc:
                _emit(job_id, {"type": "video_error", "video": vname, "error": str(exc)})
                results.append({"video": vname, "ok": False, "error": str(exc)})
                continue

            if out is not None:
                r: dict = {"video": vname, "output": out.name, "ok": True}
                cs = output_dir / f"{vpath.stem}_contact.png"
                if ns.contact_sheet and cs.is_file():
                    r["contact_sheet"] = cs.name
                results.append(r)
                _emit(job_id, {"type": "video_done", **r})
            elif ns.preview:
                detect_png = output_dir / f"{vpath.stem}_detect.png"
                if detect_png.is_file():
                    r = {"video": vname, "output": detect_png.name, "ok": True, "is_preview": True}
                    results.append(r)
                    _emit(job_id, {"type": "video_done", **r})
                else:
                    results.append({"video": vname, "ok": False})
                    _emit(job_id, {"type": "video_error", "video": vname, "error": "Preview image not generated"})
            else:
                results.append({"video": vname, "ok": False})
                _emit(job_id, {"type": "video_error", "video": vname, "error": "Processing returned no output"})
    finally:
        ls_log.removeHandler(handler)
        with _lock:
            _jobs[job_id]["results"] = results
            _jobs[job_id]["status"] = "done"


def _find_upload(file_id: str) -> Optional[dict]:
    d = WORK_DIR / str(file_id)
    if not d.is_dir():
        return None
    files = [f for f in d.iterdir() if f.is_file()]
    return {"name": files[0].name, "id": file_id, "path": str(files[0])} if files else None


def _parse_region(s) -> Optional[list]:
    if not s or not str(s).strip():
        return None
    try:
        parts = [int(x) for x in str(s).split()]
        return parts if len(parts) == 4 else None
    except ValueError:
        return None


def _to_float(v) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


app.register_blueprint(bp, url_prefix=_BASE_PATH)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time
    import webbrowser

    print()
    print("  +--------------------------------------+")
    print("  |   logoswap  -  Web UI               |")
    print("  |   http://localhost:5000             |")
    print("  +--------------------------------------+")
    print()
    print("  Requirements:  pip install flask numpy Pillow opencv-python")
    print("  Also needed:   ffmpeg + ffprobe on PATH")
    print()

    threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open("http://localhost:5000")), daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
