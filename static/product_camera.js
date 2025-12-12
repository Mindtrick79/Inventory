(function () {
  function _q(id) {
    return document.getElementById(id);
  }

  function setStatus(statusEl, msg) {
    if (!statusEl) return;
    statusEl.textContent = msg || '';
  }

  function setStatusWithHttpsHint(statusEl, msg) {
    if (!statusEl) return;

    // Only offer an https:// link when the page is currently http:// and not localhost.
    var host = (window.location && window.location.hostname) ? window.location.hostname : '';
    var isLocal = (host === 'localhost' || host === '127.0.0.1');
    var isHttp = (window.location && window.location.protocol === 'http:');

    statusEl.textContent = '';

    var wrap = document.createElement('div');
    wrap.appendChild(document.createTextNode(msg || ''));

    if (isHttp && !isLocal) {
      var httpsUrl = window.location.href.replace(/^http:/i, 'https:');
      var sep = document.createElement('span');
      sep.appendChild(document.createTextNode(' '));
      wrap.appendChild(sep);

      var link = document.createElement('a');
      link.href = httpsUrl;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Open HTTPS version';
      wrap.appendChild(link);
    }

    statusEl.appendChild(wrap);
  }

  function renderHttpsHelp(helpEl) {
    if (!helpEl) return;

    var host = (window.location && window.location.hostname) ? window.location.hostname : '';
    var isLocal = (host === 'localhost' || host === '127.0.0.1');
    var isHttp = (window.location && window.location.protocol === 'http:');

    // Only show on insecure contexts for non-localhost.
    if (!isHttp || isLocal || window.isSecureContext !== false) {
      helpEl.textContent = '';
      return;
    }

    helpEl.textContent = '';

    var title = document.createElement('div');
    title.className = 'camera-help__title';
    title.textContent = 'Camera needs HTTPS';
    helpEl.appendChild(title);

    var body = document.createElement('div');
    body.className = 'camera-help__body';
    body.appendChild(document.createTextNode('Modern browsers only allow webcam access on secure contexts (HTTPS or localhost).'));
    helpEl.appendChild(body);

    var actions = document.createElement('div');
    actions.className = 'camera-help__actions';

    var httpsUrl = window.location.href.replace(/^http:/i, 'https:');
    var openLink = document.createElement('a');
    openLink.href = httpsUrl;
    openLink.target = '_blank';
    openLink.rel = 'noopener';
    openLink.className = 'btn btn--secondary btn--sm';
    openLink.textContent = 'Open HTTPS version';
    actions.appendChild(openLink);

    var readmeLink = document.createElement('a');
    readmeLink.href = 'https://github.com/Mindtrick79/Inventory#https-required-for-camera-on-laptopsphones';
    readmeLink.target = '_blank';
    readmeLink.rel = 'noopener';
    readmeLink.className = 'btn btn--ghost btn--sm';
    readmeLink.textContent = 'HTTPS setup guide';
    actions.appendChild(readmeLink);

    helpEl.appendChild(actions);

    var hint = document.createElement('div');
    hint.className = 'camera-help__hint';
    hint.textContent = 'On Raspberry Pi, use Caddy reverse proxy with tls internal to serve https://<PI_IP>/ and keep Flask on :8000.';
    helpEl.appendChild(hint);
  }

  function stopStream(stream) {
    try {
      if (stream) {
        stream.getTracks().forEach(function (t) {
          t.stop();
        });
      }
    } catch (e) {
      // ignore
    }
  }

  function ensureModal(opts) {
    var existing = document.getElementById(opts.modalId);
    if (existing) return existing;

    var modal = document.createElement('div');
    modal.id = opts.modalId;
    modal.className = 'camera-modal';
    modal.innerHTML = '' +
      '<div class="camera-modal__panel">' +
        '<div class="camera-modal__header">' +
          '<div class="camera-modal__title">Take Photo</div>' +
          '<button type="button" class="camera-modal__close" id="' + opts.closeBtnId + '">Close</button>' +
        '</div>' +
        '<div class="camera-modal__body">' +
          '<video id="' + opts.videoId + '" autoplay playsinline></video>' +
          '<canvas id="' + opts.canvasId + '" style="display:none;"></canvas>' +
        '</div>' +
        '<div class="camera-modal__actions">' +
          '<button type="button" id="' + opts.snapBtnId + '">Snap</button>' +
          '<button type="button" id="' + opts.useBtnId + '" disabled>Use Photo</button>' +
        '</div>' +
        '<div class="camera-modal__hint" id="' + opts.hintId + '"></div>' +
      '</div>';

    document.body.appendChild(modal);
    return modal;
  }

  window.initProductCameraCapture = function initProductCameraCapture(config) {
    var opts = config || {};
    opts.inputId = opts.inputId || 'product_image';
    opts.openBtnId = opts.openBtnId || 'camera_open_btn';
    opts.clearBtnId = opts.clearBtnId || 'camera_clear_btn';
    opts.statusId = opts.statusId || 'camera_status';
    opts.httpsHelpId = opts.httpsHelpId || 'camera_https_help';

    opts.modalId = opts.modalId || 'camera_modal';
    opts.closeBtnId = opts.closeBtnId || 'camera_close_btn';
    opts.videoId = opts.videoId || 'camera_video';
    opts.canvasId = opts.canvasId || 'camera_canvas';
    opts.snapBtnId = opts.snapBtnId || 'camera_snap_btn';
    opts.useBtnId = opts.useBtnId || 'camera_use_btn';
    opts.hintId = opts.hintId || 'camera_hint';

    var input = _q(opts.inputId);
    var openBtn = _q(opts.openBtnId);
    var clearBtn = _q(opts.clearBtnId);
    var statusEl = _q(opts.statusId);
    var httpsHelpEl = _q(opts.httpsHelpId);

    if (!input || !openBtn || !clearBtn) return;

    renderHttpsHelp(httpsHelpEl);

    function clearImage() {
      try {
        input.value = '';
        setStatus(statusEl, '');
      } catch (e) {
        setStatus(statusEl, 'Unable to clear image.');
      }
    }

    clearBtn.addEventListener('click', function () {
      clearImage();
    });

    openBtn.addEventListener('click', async function () {
      renderHttpsHelp(httpsHelpEl);
      if (window.isSecureContext === false) {
        setStatusWithHttpsHint(statusEl, 'Camera requires HTTPS (or localhost).');
        input.click();
        return;
      }
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        setStatus(statusEl, 'Camera API unavailable in this browser. Try Chrome/Edge/Firefox, or use the file picker to upload.');
        input.click();
        return;
      }

      var modal = ensureModal(opts);
      var closeBtn = _q(opts.closeBtnId);
      var video = _q(opts.videoId);
      var canvas = _q(opts.canvasId);
      var snapBtn = _q(opts.snapBtnId);
      var useBtn = _q(opts.useBtnId);
      var hint = _q(opts.hintId);

      var activeStream = null;
      var lastBlob = null;
      if (useBtn) useBtn.disabled = true;
      if (hint) hint.textContent = '';

      function close() {
        modal.classList.remove('camera-modal--open');
        stopStream(activeStream);
        activeStream = null;
        lastBlob = null;
        if (useBtn) useBtn.disabled = true;
      }

      if (closeBtn) {
        closeBtn.onclick = close;
      }
      modal.addEventListener('click', function (e) {
        if (e.target === modal) close();
      });

      try {
        activeStream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: 'environment' } },
          audio: false,
        });
        if (video) {
          video.srcObject = activeStream;
        }
        modal.classList.add('camera-modal--open');
      } catch (e) {
        setStatus(statusEl, 'Camera permission denied or unavailable. Use the file picker to upload.');
        input.click();
        return;
      }

      if (snapBtn) {
        snapBtn.onclick = function () {
          if (!video || !canvas) return;
          var w = video.videoWidth || 1280;
          var h = video.videoHeight || 720;
          canvas.width = w;
          canvas.height = h;
          var ctx = canvas.getContext('2d');
          ctx.drawImage(video, 0, 0, w, h);
          canvas.toBlob(function (blob) {
            lastBlob = blob;
            if (useBtn) useBtn.disabled = !blob;
            if (hint) hint.textContent = blob ? 'Photo captured. Click “Use Photo”.' : 'Failed to capture photo.';
          }, 'image/jpeg', 0.9);
        };
      }

      if (useBtn) {
        useBtn.onclick = function () {
          if (!lastBlob) return;
          var file = new File([lastBlob], 'camera_photo.jpg', { type: 'image/jpeg' });
          try {
            var dt = new DataTransfer();
            dt.items.add(file);
            input.files = dt.files;
            setStatus(statusEl, 'Camera photo selected.');
          } catch (e) {
            setStatus(statusEl, 'Camera photo captured, but browser could not attach it. Please use upload instead.');
          }
          close();
        };
      }
    });
  };
})();
