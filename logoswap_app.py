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
.nav-logo { font-size: 28px; line-height: 1; }
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
  <span class="nav-logo">🎬</span>
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


def _index_html() -> str:
    return _HTML.replace(
        "<head>",
        f"<head><script>window.__APP_BASE_PATH__={json.dumps(_BASE_PATH)};</script>",
        1,
    )


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
