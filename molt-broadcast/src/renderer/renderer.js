'use strict';

let callObject = null;
let activeStream = null;
let roomUrl = null;
let ownerToken = null;

const statusEl = document.getElementById('status');
const roomInfoEl = document.getElementById('room-info');
const roomUrlEl = document.getElementById('room-url');
const copyBtn = document.getElementById('copy-btn');
const startBtn = document.getElementById('start-btn');
const stopBtn = document.getElementById('stop-btn');
const previewEl = document.getElementById('preview');
const previewContainer = document.getElementById('preview-container');
const liveBadge = document.getElementById('live-badge');

function setStatus(msg) {
  statusEl.textContent = msg;
}

async function init() {
  setStatus('Creating Daily room...');
  try {
    const { url, token } = await window.electronAPI.createRoom();
    roomUrl = url;
    ownerToken = token;
    roomUrlEl.value = url;
    roomInfoEl.classList.remove('hidden');
    setStatus('Room ready. Click Start Streaming to go live.');
    startBtn.disabled = false;

    callObject = DailyIframe.createCallObject({ strictMode: false });
    callObject.on('error', (e) => setStatus(`Daily error: ${e.errorMsg}`));
  } catch (err) {
    setStatus(`Error: ${err.message || String(err)}`);
    console.error(err);
  }
}

startBtn.addEventListener('click', async () => {
  startBtn.disabled = true;
  setStatus('Capturing screen...');

  try {
    const sources = await window.electronAPI.getSources();
    if (!sources.length) throw new Error('No screen sources found');

    const sourceId = sources[0].id;

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        mandatory: {
          chromeMediaSource: 'desktop',
          chromeMediaSourceId: sourceId,
          maxFrameRate: 15,
        },
      },
    });

    activeStream = stream;
    previewEl.srcObject = stream;
    previewContainer.classList.remove('hidden');

    setStatus('Joining room...');
    const videoTrack = stream.getVideoTracks()[0];
    await callObject.join({ url: roomUrl, token: ownerToken, startVideoOff: false, startAudioOff: true });
    await callObject.setInputDevicesAsync({ videoSource: videoTrack });

    setStatus('Streaming live.');
    liveBadge.classList.remove('hidden');
    stopBtn.classList.remove('hidden');
  } catch (err) {
    setStatus(`Stream error: ${err.message || String(err)}`);
    console.error(err);
    if (activeStream) {
      activeStream.getTracks().forEach((t) => t.stop());
      activeStream = null;
    }
    previewEl.srcObject = null;
    previewContainer.classList.add('hidden');
    startBtn.disabled = false;
  }
});

stopBtn.addEventListener('click', async () => {
  if (activeStream) {
    activeStream.getTracks().forEach((t) => t.stop());
    activeStream = null;
  }
  if (callObject) {
    try { await callObject.leave(); } catch (_) {}
    callObject = DailyIframe.createCallObject({ strictMode: false });
    callObject.on('error', (e) => setStatus(`Daily error: ${e.errorMsg}`));
  }

  previewEl.srcObject = null;
  previewContainer.classList.add('hidden');
  liveBadge.classList.add('hidden');
  stopBtn.classList.add('hidden');
  startBtn.disabled = false;
  setStatus('Stopped. Click Start Streaming to go live again.');
});

copyBtn.addEventListener('click', () => {
  navigator.clipboard.writeText(roomUrlEl.value).then(() => {
    copyBtn.textContent = 'Copied!';
    setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
  });
});

init();
