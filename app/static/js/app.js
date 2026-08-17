/* BoxMedia progressive enhancement — the app works without this file.
 *
 * Settings actions are POST -> 303 -> GET (so the reloaded page shows fresh connection
 * health, app lists and backup rows). That reload otherwise dumps the admin back at the
 * top of a long page. Here we remember the scroll offset across the redirect and treat
 * the status message as a one-shot toast rather than a banner that lingers until you
 * navigate away. Loaded as a same-origin file because the CSP is script-src 'self'.
 */
(function () {
  "use strict";

  var SCROLL_KEY = "bm_scroll";
  var FRAGMENT_PARAM = "fragment=1";
  var STATUS_PARAM = "status";
  var TOAST_REMOVE_MS = 3400; // matches the CSS fade-out end (3s hold + 0.3s fade)
  var TESTING_LABEL = "Testing…";

  function readStored(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch (error) {
      return null; // private mode / storage disabled — degrade to no scroll restore
    }
  }

  function writeStored(key, value) {
    try {
      window.sessionStorage.setItem(key, value);
    } catch (error) {
      /* ignore — restoring scroll is a nicety, never a requirement */
    }
  }

  function clearStored(key) {
    try {
      window.sessionStorage.removeItem(key);
    } catch (error) {
      /* ignore */
    }
  }

  // Capture phase so the offset is stored even when a handler stops propagation.
  document.addEventListener(
    "submit",
    function () {
      writeStored(SCROLL_KEY, String(window.scrollY || window.pageYOffset || 0));
    },
    true
  );

  var savedOffset = readStored(SCROLL_KEY);
  if (savedOffset !== null) {
    clearStored(SCROLL_KEY);
    var offset = parseInt(savedOffset, 10);
    if (!isNaN(offset) && offset > 0) {
      window.scrollTo(0, offset);
    }
  }

  // All of them, not just the first: the region stacks a status confirmation and the
  // one-shot sign-in notice, and querySelector would leave the second one in the DOM.
  var toasts = document.querySelectorAll("[data-toast]");
  if (toasts.length) {
    // A status code is a one-shot confirmation: drop it from the URL so reloading or
    // sharing the address doesn't replay the message.
    if (window.history && window.history.replaceState) {
      var url = new URL(window.location.href);
      if (url.searchParams.has(STATUS_PARAM)) {
        url.searchParams.delete(STATUS_PARAM);
        var query = url.searchParams.toString();
        window.history.replaceState(null, "", url.pathname + (query ? "?" + query : "") + url.hash);
      }
    }
    window.setTimeout(function () {
      Array.prototype.forEach.call(toasts, function (toast) {
        if (toast.parentNode) {
          toast.parentNode.removeChild(toast);
        }
      });
    }, TOAST_REMOVE_MS);
  }

  /* Poster -> movie detail. The poster is a real link to the same route, so with this
   * file blocked (or <dialog> unsupported) the click simply navigates to the page. */
  var dialog = document.getElementById("movie-dialog");
  var dialogBody = dialog && dialog.querySelector("[data-movie-body]");

  function openModal(url, fallbackHref) {
    dialogBody.textContent = "Loading…";
    dialog.showModal();
    window
      .fetch(url, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.text();
      })
      .then(function (html) {
        // Server-rendered, same-origin, already autoescaped by Jinja.
        dialogBody.innerHTML = html;
        dialog.scrollTop = 0;
      })
      .catch(function () {
        // Never strand the user in an empty modal — fall back to the real page.
        dialog.close();
        window.location.href = fallbackHref;
      });
  }

  // "?fragment=1" or "&fragment=1", depending on whether the URL already has a query.
  function asFragment(url) {
    return url + (url.indexOf("?") === -1 ? "?" : "&") + FRAGMENT_PARAM;
  }

  if (dialog && dialogBody && typeof dialog.showModal === "function" && window.fetch) {
    document.addEventListener("click", function (event) {
      // Let modified clicks (new tab, download, middle click) behave normally.
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey ||
          event.shiftKey || event.altKey) {
        return;
      }
      var link = event.target.closest("a[data-movie]");
      if (!link || !link.getAttribute("href")) {
        return;
      }
      event.preventDefault();
      openModal(asFragment(link.getAttribute("href")), link.getAttribute("href"));
    });

    /* A form marked data-modal shows its result in the dialog instead of navigating.
     * Without this file the submit is an ordinary GET to the same URL, which renders
     * the identical fragment inside a normal page. */
    document.addEventListener("submit", function (event) {
      var form = event.target.closest && event.target.closest("form[data-modal]");
      if (!form || event.defaultPrevented) {
        return;
      }
      event.preventDefault();
      var query = new URLSearchParams(new FormData(form)).toString();
      var href = form.getAttribute("action") + (query ? "?" + query : "");
      openModal(asFragment(href), href);
    });

    // Clicking the backdrop (the dialog element itself, outside its content) closes it.
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog) {
        dialog.close();
      }
    });
    dialog.addEventListener("close", function () {
      dialogBody.textContent = "";
    });
  }
  /* Test Connection in the Add form: probe what has been typed, without saving it.
   *
   * The button ships hidden and is revealed here, because the no-JavaScript version of
   * this would have to re-render the form with the typed API key echoed back into the
   * HTML. Those users are not stranded: Add itself reports whether the connection
   * answered.
   *
   * The reply is a server-rendered fragment swapped in whole — no markup is built here,
   * so escaping stays where the rest of the app does it. */
  var testButton = document.querySelector("[data-test-connection]");
  var testForm = testButton && testButton.closest("form");
  var testSlot = testForm && testForm.querySelector("[data-test-result]");

  if (testButton && testForm && testSlot && window.fetch) {
    testButton.hidden = false;
    testButton.addEventListener("click", function (event) {
      event.preventDefault();
      if (testButton.disabled) {
        return;
      }
      var label = testButton.textContent;
      testButton.disabled = true;
      testButton.textContent = TESTING_LABEL;
      // Clear the previous verdict: leaving a stale green line beside a freshly edited
      // address would say the wrong thing about what is now in the form.
      testSlot.textContent = "";

      fetch(testButton.getAttribute("formaction"), {
        method: "POST",
        body: new FormData(testForm),
        credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" }
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("test failed");
          }
          return response.text();
        })
        .then(function (html) {
          testSlot.innerHTML = html;
        })
        .catch(function () {
          // Say nothing rather than something wrong: a failed request proves nothing
          // about the connection being tested.
          testSlot.textContent = "";
        })
        .then(function () {
          testButton.disabled = false;
          testButton.textContent = label;
        });
    });
  }

  /* Back to the top of a long grid.
   *
   * Shown only once there is a screenful to come back from, and the threshold is the
   * VIEWPORT's own height rather than a fixed pixel count — so a 5" phone in portrait and
   * a 17" laptop each reveal it after exactly one screen, which is what "when needed"
   * means on either. A page too short to scroll never shows it at all.
   *
   * The listener is passive (it never calls preventDefault, and saying so lets the browser
   * keep scrolling off the main thread) and writes to the DOM only when the answer
   * actually changes — so a flung touch scroll costs one comparison per event and one
   * attribute write per crossing, not one write per event. No requestAnimationFrame: for a
   * handler this small the scheduling would cost more than the work, and it would put the
   * only interesting behaviour behind a callback that never runs in a headless browser.
   */
  var toTop = document.querySelector("[data-to-top]");
  if (toTop) {
    var reducedMotion =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var shown = false;

    function syncToTop() {
      var offset = window.scrollY || window.pageYOffset || 0;
      var wanted = offset >= window.innerHeight;
      if (wanted !== shown) {
        shown = wanted;
        toTop.hidden = !wanted;
      }
    }

    window.addEventListener("scroll", syncToTop, { passive: true });
    // Rotating a tablet changes innerHeight, and with it whether the button is warranted.
    window.addEventListener("resize", syncToTop, { passive: true });
    syncToTop();

    toTop.addEventListener("click", function () {
      // Smooth by default, instant for a reader who asked for less motion — an animated
      // scroll across several screens is exactly what that setting is about.
      if (reducedMotion || !("scrollBehavior" in document.documentElement.style)) {
        window.scrollTo(0, 0);
      } else {
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    });
  }

  /* Keeping a download's progress current without a reload.
   *
   * The one thing a rendered page cannot do for itself: 40% is not 40% a minute later.
   * Every indicator carries `data-progress="<connection>:<radarr id>"`, and /progress
   * answers with the same keys — so one updater drives a dashboard chip, a weekly card's
   * line and the movie modal's held line without knowing which it is looking at.
   *
   * Polite about it, because each poll is one queue request per configured Radarr:
   *   - nothing on the page to update -> never starts;
   *   - tab in the background -> stops, and catches up when it comes back;
   *   - nothing left downloading -> stops until the next page load.
   * Fifteen seconds because that is the granularity the figure actually has; faster only
   * redraws the same number.
   */
  // Read from the DOM, never built here: this file has no idea what url_base is, and
  // every other URL it uses comes off an element for the same reason.
  var PROGRESS_URL = document.body.getAttribute("data-progress-url");
  var PROGRESS_INTERVAL_MS = 15000;
  var progressTimer = null;

  function progressNodes() {
    return document.querySelectorAll("[data-progress]");
  }

  function paintProgress(node, percent) {
    // The chip carries its fill as one of eleven literal classes (a per-film width would
    // have to be an inline style, and the CSP forbids one), so the step is recomputed the
    // same way the server does it.
    if (node.className && node.className.indexOf("where-chip") !== -1) {
      node.className = node.className.replace(/\s*where-chip-p\d+/g, "");
      if (percent !== null) {
        node.className += " where-chip-p" + String(Math.round(percent / 10) * 10);
      }
      var base = node.getAttribute("data-progress-title") || "";
      node.setAttribute(
        "title", percent === null ? base : base + " — " + String(Math.round(percent)) + "% downloaded"
      );
      return;
    }
    var slot = node.querySelector("[data-progress-percent]");
    if (slot) {
      // A finished download is not 0% — it has simply stopped being one, and the next
      // page load shows it as held. Until then the line stops claiming a figure.
      if (percent === null) {
        node.hidden = true;
      } else {
        node.hidden = false;
        slot.textContent = String(Math.round(percent));
      }
    }
  }

  function applyProgress(live) {
    var remaining = 0;
    Array.prototype.forEach.call(progressNodes(), function (node) {
      var key = node.getAttribute("data-progress");
      var percent = Object.prototype.hasOwnProperty.call(live, key) ? live[key] : null;
      paintProgress(node, percent);
      if (percent !== null) {
        remaining += 1;
      }
    });
    return remaining;
  }

  function pollProgress() {
    if (document.hidden) {
      return; // a background tab must not keep asking Radarr
    }
    window
      .fetch(PROGRESS_URL, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.json();
      })
      .then(function (live) {
        if (applyProgress(live) === 0) {
          stopProgress(); // everything landed; nothing left to watch
        }
      })
      .catch(function () {
        /* A failed poll says nothing about the download — leave the last figures alone
         * and try again on the next tick. */
      });
  }

  function startProgress() {
    if (progressTimer === null && progressNodes().length) {
      progressTimer = window.setInterval(pollProgress, PROGRESS_INTERVAL_MS);
    }
  }

  function stopProgress() {
    if (progressTimer !== null) {
      window.clearInterval(progressTimer);
      progressTimer = null;
    }
  }

  if (PROGRESS_URL && window.fetch && progressNodes().length) {
    startProgress();
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        stopProgress();
      } else {
        pollProgress(); // catch up on what changed while the tab was away
        startProgress();
      }
    });
  }
})();
