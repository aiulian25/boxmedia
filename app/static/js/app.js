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
  var SAVING_LABEL = "Saving…";
  var SAVED_ALL_STATUS = "settings_saved";
  var STATUS_KIND_SUCCESS = "success";
  var SAVED_PREFIX = "Saved: ";
  var NOT_SAVED_PREFIX = "Not saved: ";
  var NOTHING_SAVED_PREFIX = "Nothing was saved. Check ";
  var INVALID_SUFFIX = ", which has a field that needs correcting.";
  var SAVE_REFUSED_REASON =
    "BoxMedia refused this. Your edits are still here, so note them before reloading if it keeps happening.";
  var SESSION_ENDED_REASON =
    "Your session has ended, so this was not saved. Sign in again in another tab, then press Save here.";
  var CSRF_REFUSED_STATUS = 403;
  var SAVE_UNCONFIRMED_REASON =
    "BoxMedia did not confirm this was saved. Reload the page to check what is stored.";
  var UNREACHABLE_REASON = "Could not reach BoxMedia. Your edits are still here, so try again.";
  // Marks the history entry the modal pushes for itself, so Back closes the modal
  // instead of leaving the shelf the reader was on.
  var MODAL_STATE_KEY = "boxmediaModal";
  var DIALOG_HEADING_SELECTOR = "h1, h2, h3";

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

  // Set while the fallback below is navigating away, so closing the dialog on the way
  // out does not also fire a history.back() and race the assignment.
  var leavingForFallback = false;

  // The request allowed to speak for the dialog. Only ever the newest one: the dialog is
  // shared, so a slow answer about the last title would otherwise replace this title, and
  // a slow failure would navigate away from it.
  var currentOpening = null;
  var dialogLoadingLabel = dialog && dialog.getAttribute("aria-label");

  function abandonOpening() {
    if (currentOpening) {
      currentOpening.abort();
      currentOpening = null;
    }
  }

  function openModal(url, fallbackHref) {
    abandonOpening();
    var opening = new AbortController();
    currentOpening = opening;
    var stillCurrent = function () {
      return currentOpening === opening && dialog.open;
    };
    dialogBody.textContent = "Loading…";
    dialog.setAttribute("aria-label", dialogLoadingLabel);
    dialog.showModal();
    // A history entry of its own, at the SAME url — the address bar does not change,
    // but Back now has something to consume. Without it, Back dismisses the modal by
    // leaving the page, which drops the reader on whichever chip they were on before
    // this one rather than the shelf they are actually looking at.
    if (window.history && window.history.pushState) {
      var pushed = {};
      pushed[MODAL_STATE_KEY] = true;
      window.history.pushState(pushed, "");
    }
    window
      .fetch(url, { credentials: "same-origin", signal: opening.signal })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.text();
      })
      .then(function (html) {
        if (!stillCurrent()) {
          return;
        }
        // Server-rendered, same-origin, already autoescaped by Jinja.
        dialogBody.innerHTML = html;
        dialog.scrollTop = 0;
        var heading = dialogBody.querySelector(DIALOG_HEADING_SELECTOR);
        if (heading) {
          dialog.setAttribute("aria-label", heading.textContent.trim());
        }
      })
      .catch(function () {
        if (!stillCurrent()) {
          return;
        }
        // Never strand the user in an empty modal — fall back to the real page.
        leavingForFallback = true;
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
      // Both hooks, one handler. A series opens in the same dialog as a film because it
      // is the same gesture — the only difference is which route renders the fragment,
      // and that is already in the link's own href.
      var link = event.target.closest("a[data-movie], a[data-show]");
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
      // This event is queued, so it can arrive after the next title has already opened.
      if (dialog.open) {
        return;
      }
      abandonOpening();
      dialogBody.textContent = "";
      // Closed by Escape, the × or the backdrop: the entry opening it pushed is still
      // current, so spend it. Otherwise the reader would have to press Back twice to
      // leave a shelf they never navigated away from.
      var state = window.history && window.history.state;
      if (!leavingForFallback && state && state[MODAL_STATE_KEY]) {
        window.history.back();
      }
    });

    // Back closes the modal rather than the page. The entry is already popped by the
    // time this runs, so the close handler above finds no modal state and does not
    // push the history back the other way.
    window.addEventListener("popstate", function () {
      if (dialog.open) {
        dialog.close();
      }
    });
  }
  /* Test Connection: probe what has been typed, without saving it.
   *
   * Every button carrying [data-test-connection] is wired, not just the first — this
   * was a `querySelector` when the Radarr Add form was the only one, and the Plex card
   * arriving second would have left one of the two dead depending on DOM order.
   *
   * The buttons ship hidden and are revealed here, because the no-JavaScript version
   * would have to re-render the form with the typed secret echoed back into the HTML.
   * Those users are not stranded: saving reports whether the connection answered.
   *
   * The reply is a server-rendered fragment swapped in whole — no markup is built here,
   * so escaping stays where the rest of the app does it. */
  var testButtons = document.querySelectorAll("[data-test-connection]");
  Array.prototype.forEach.call(testButtons, function (testButton) {
    var testForm = testButton.closest("form");
    var testSlot = testForm && testForm.querySelector("[data-test-result]");
    if (!testForm || !testSlot || !window.fetch) {
      return;
    }
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

      // A native submission includes the pressed button's own name/value; `new
      // FormData(form)` does not. Added back explicitly rather than with FormData's
      // two-argument form, which is recent enough to leave older browsers silently
      // posting a body with a field missing. This is how one form can carry two Test
      // buttons that mean different things — the Discovery card's TMDB and Trakt.
      var body = new FormData(testForm);
      if (testButton.name) {
        body.append(testButton.name, testButton.value);
      }

      fetch(testButton.getAttribute("formaction"), {
        method: "POST",
        body: body,
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
  });

  /* Which service is being configured, said in every field that depends on it.
   *
   * Two cards ask this question — Plex vs Jellyfin on the media server, Radarr vs
   * Sonarr on Add New App — and the answer changes the same three kinds of thing: the
   * port in the address, the name of a field, and where to find the credential. ONE
   * mechanism, configured twice, so a third service is a config entry rather than a
   * third copy of this loop.
   *
   * Every string comes off a data- attribute rendered by the template, so nothing here
   * invents copy, nothing is built by string-concatenation, and the no-JavaScript page
   * still names both options in its static placeholder.
   *
   * `textContent`, never innerHTML: these values are template-authored, but writing
   * markup from script is how that stops being true later. */
  var wireKindSwap = function (radioAttribute, fields) {
    var radios = document.querySelectorAll("[" + radioAttribute + "]");
    if (!radios.length) {
      return;
    }
    var apply = function (kind) {
      for (var index = 0; index < fields.length; index++) {
        var field = fields[index];
        var element = document.querySelector(field.selector);
        if (!element) {
          continue;
        }
        var value = element.getAttribute(field.prefix + kind);
        if (!value) {
          continue;
        }
        if (field.property === "placeholder") {
          element.placeholder = value;
        } else {
          element.textContent = value;
        }
      }
    };
    Array.prototype.forEach.call(radios, function (radio) {
      if (radio.checked) {
        apply(radio.value);
      }
      radio.addEventListener("change", function () {
        if (radio.checked) {
          apply(radio.value);
        }
      });
    });
  };

  wireKindSwap("data-server-kind", [
    { selector: "#server-url", prefix: "data-placeholder-", property: "placeholder" },
    { selector: "[data-secret-label]", prefix: "data-label-", property: "text" },
    { selector: "[data-secret-hint]", prefix: "data-hint-", property: "text" }
  ]);
  wireKindSwap("data-app-kind", [
    { selector: "#new-url", prefix: "data-placeholder-", property: "placeholder" },
    { selector: "[data-app-hint]", prefix: "data-hint-", property: "text" }
  ]);

  /* One Save for the whole Settings page.
   *
   * The page is built from separate forms because each posts to the route that owns its
   * fields — that is what keeps saving the region from touching the backup cadence. But
   * a Save button per card asks the reader to know that split, which is this app's
   * problem and not theirs. So: the per-card buttons are hidden here, a bar appears the
   * moment anything changes, and Save submits exactly the forms that were edited.
   *
   * Hidden HERE rather than in the template, so the page without JavaScript keeps every
   * button it has always had. That is the same bargain the Test buttons make in the
   * other direction.
   *
   * Only forms marked [data-settings-form] take part. The others on this page are
   * ACTIONS, not settings — restore a backup, delete a connection, change a password,
   * un-ignore a title. None of them is a field whose value could be "saved later", and
   * a password in particular is a deliberate act that asks for the current one. */
  var settingsForms = document.querySelectorAll("[data-settings-form]");
  var saveBar = document.querySelector("[data-save-bar]");

  if (settingsForms.length && saveBar && window.fetch) {
    // What each form held when the server rendered it. A field edited and then put back
    // is not a change, and neither is anything a password manager fills in and out — the
    // bar answers "does this differ from what is stored", not "did an event fire".
    var initial = new WeakMap();
    var dirty = [];
    var saveButton = saveBar.querySelector("[data-save-all]");
    var discardButton = saveBar.querySelector("[data-discard-all]");
    // Where to reload once everything is written. Read off the element, never built
    // here: this file has no idea what url_base is, which is why every address it uses
    // comes from the markup.
    var settingsUrl = saveBar.getAttribute("data-settings-url");
    var saveBarText = saveBar.querySelector("[data-save-text]");
    var saveBarDefaultText = saveBarText.textContent;
    var saveLabel = saveButton.textContent;
    // The server's own table of what each status means, so there is one list of them.
    var saveStatuses = JSON.parse(saveBar.getAttribute("data-save-statuses"));
    var savedDuringThisPage = false;

    var showBar = function (visible) {
      saveBar.hidden = !visible;
      document.body.classList.toggle("has-save-bar", visible);
    };

    var snapshot = function (form) {
      // FormData preserves field order, which is stable for a form nobody reshapes.
      var pairs = [];
      new FormData(form).forEach(function (value, name) {
        pairs.push(name + "=" + value);
      });
      return pairs.join("&");
    };

    var recheck = function (form) {
      var changed = snapshot(form) !== initial.get(form);
      var at = dirty.indexOf(form);
      if (changed && at === -1) {
        dirty.push(form);
      } else if (!changed && at !== -1) {
        dirty.splice(at, 1);
      }
      showBar(dirty.length > 0);
    };

    Array.prototype.forEach.call(settingsForms, function (form) {
      // The card's own Save still exists for a browser without this file; with it, the
      // bar speaks for every card at once.
      var ownSave = form.querySelector("[data-form-save]");
      if (ownSave) {
        ownSave.hidden = true;
      }
      initial.set(form, snapshot(form));
      form.addEventListener("input", function () { recheck(form); });
      form.addEventListener("change", function () { recheck(form); });
      // An edit is the reader answering the error, so it stops standing over the card.
      form.addEventListener("input", function () {
        var note = form.previousElementSibling;
        if (note && note.hasAttribute("data-save-error")) {
          note.parentNode.removeChild(note);
        }
      });
    });

    discardButton.addEventListener("click", function () {
      // reset() goes back to what was rendered, which a card saved since no longer matches.
      if (savedDuringThisPage) {
        window.location.href = settingsUrl;
        return;
      }
      // Back to what the server rendered, which for every field on this page is what is
      // stored. No request: discarding a change that was never sent is a local matter.
      Array.prototype.forEach.call(dirty, function (form) { form.reset(); });
      dirty = [];
      clearErrors();
      saveBarText.textContent = saveBarDefaultText;
      showBar(false);
      // reset() fires no input event, so nothing above re-reads the forms; they now hold
      // exactly what was rendered, which is what `initial` already remembers.
    });

    // Why a response counts as saved. Only a known success status on the Settings page
    // itself: a login page, a proxy's page or a status nobody knows are all things that are
    // not "saved", and a save reported wrongly is worse than one reported as failed.
    var outcomeOf = function (response) {
      if (!response.ok) {
        return { saved: false, reason: SAVE_REFUSED_REASON };
      }
      var landed = new URL(response.url);
      if (landed.pathname !== new URL(settingsUrl, window.location.href).pathname) {
        return { saved: false, reason: SESSION_ENDED_REASON };
      }
      var known = saveStatuses[landed.searchParams.get(STATUS_PARAM)];
      if (!known) {
        return { saved: false, reason: SAVE_UNCONFIRMED_REASON };
      }
      return { saved: known[0] === STATUS_KIND_SUCCESS, reason: known[1] };
    };

    var clearErrors = function () {
      Array.prototype.forEach.call(document.querySelectorAll("[data-save-error]"), function (node) {
        node.parentNode.removeChild(node);
      });
    };

    var showCardError = function (form, reason) {
      var note = document.createElement("p");
      note.className = "banner-warn";
      note.setAttribute("data-save-error", "");
      note.setAttribute("role", "alert");
      // textContent, because a card's name is whatever someone called their connection.
      note.textContent = cardName(form) + ": " + reason;
      form.parentNode.insertBefore(note, form);
      note.scrollIntoView({ block: "center" });
    };

    var cardName = function (form) {
      return form.getAttribute("data-settings-form");
    };

    var stopSaving = function (message) {
      saveBarText.textContent = message;
      saveButton.disabled = false;
      discardButton.disabled = false;
      saveButton.textContent = saveLabel;
    };

    saveButton.addEventListener("click", function () {
      if (saveButton.disabled || !dirty.length) {
        return;
      }
      clearErrors();
      // All of them before any of them, so an invalid last card cannot leave the first
      // ones written and the rest not.
      var invalid = dirty.filter(function (form) { return !form.checkValidity(); })[0];
      if (invalid) {
        stopSaving(NOTHING_SAVED_PREFIX + cardName(invalid) + INVALID_SUFFIX);
        invalid.reportValidity();
        return;
      }
      saveBarText.textContent = saveBarDefaultText;
      saveButton.disabled = true;
      discardButton.disabled = true;
      saveButton.textContent = SAVING_LABEL;

      // One at a time, in page order: two of these write the same filters file, and
      // firing them together would let the later read clobber the earlier write.
      var pending = dirty.slice();
      var savedNames = [];

      // A form's token belongs to the session that rendered it, so signing back in elsewhere
      // leaves this page's stale. Take the new one from the Settings page, once per save.
      var renewedToken = false;
      var renewTokens = function () {
        return fetch(settingsUrl, { credentials: "same-origin" })
          .then(function (response) { return response.text(); })
          .then(function (html) {
            var fresh = new DOMParser()
              .parseFromString(html, "text/html")
              .querySelector("input[name=csrf_token]");
            if (!fresh) {
              throw new Error("no token");
            }
            Array.prototype.forEach.call(
              document.querySelectorAll("input[name=csrf_token]"),
              function (input) { input.value = fresh.value; }
            );
            Array.prototype.forEach.call(settingsForms, function (form) {
              if (dirty.indexOf(form) === -1) {
                initial.set(form, snapshot(form));
              }
            });
          });
      };

      var failed = function (form, reason) {
        var saved = savedNames.length ? SAVED_PREFIX + savedNames.join(", ") + ". " : "";
        stopSaving(saved + NOT_SAVED_PREFIX + cardName(form) + ".");
        showCardError(form, reason);
      };

      var next = function () {
        var form = pending.shift();
        if (!form) {
          // Reload so every card shows what is stored rather than what was typed.
          window.location.href = settingsUrl + "?" + STATUS_PARAM + "=" +
            encodeURIComponent(SAVED_ALL_STATUS);
          return;
        }
        fetch(form.getAttribute("action"), {
          method: "POST",
          body: new FormData(form),
          credentials: "same-origin",
          headers: { "X-Requested-With": "fetch" }
        }).then(
          function (response) {
            if (response.status === CSRF_REFUSED_STATUS && !renewedToken) {
              renewedToken = true;
              pending.unshift(form);
              renewTokens().then(next, function () {
                pending.shift();
                failed(form, SAVE_REFUSED_REASON);
              });
              return;
            }
            var outcome = outcomeOf(response);
            if (!outcome.saved) {
              failed(form, outcome.reason);
              return;
            }
            // This one is stored now: it stops being a pending edit, so trying again after
            // a later card's failure does not post it twice.
            initial.set(form, snapshot(form));
            dirty.splice(dirty.indexOf(form), 1);
            savedNames.push(cardName(form));
            savedDuringThisPage = true;
            next();
          },
          function () {
            failed(form, UNREACHABLE_REASON);
          }
        );
      };
      next();
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
   *   - nothing on the page to update -> does not start, until something arrives;
   *   - tab in the background -> stops, and catches up when it comes back;
   *   - a server not answering -> keeps its last figures, and asks less often;
   *   - nothing left downloading -> stops, until something arrives.
   * Fifteen seconds because that is the granularity the figure actually has; faster only
   * redraws the same number.
   */
  // Read from the DOM, never built here: this file has no idea what url_base is, and
  // every other URL it uses comes off an element for the same reason.
  var PROGRESS_URL = document.body.getAttribute("data-progress-url");
  var PROGRESS_INTERVAL_MS = 15000;
  var PROGRESS_LONGEST_INTERVAL_MS = 120000;
  var progressTimer = null;
  var progressFailures = 0;

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

  function isConfirmedGone(key, sources) {
    // Only a connection that answered with its whole queue can say a film has left it. One
    // that is down, or whose queue is longer than the page, can only say it does not know.
    var source = sources[key.slice(0, key.indexOf(":"))];
    return Boolean(source && source.available && source.complete);
  }

  function applyProgress(answer) {
    var remaining = 0;
    Array.prototype.forEach.call(progressNodes(), function (node) {
      var key = node.getAttribute("data-progress");
      if (Object.prototype.hasOwnProperty.call(answer.progress, key)) {
        paintProgress(node, answer.progress[key]);
        remaining += 1;
      } else if (isConfirmedGone(key, answer.sources)) {
        paintProgress(node, null);
      } else {
        remaining += 1; // the last figure stands, and is still worth asking about
      }
    });
    return remaining;
  }

  function everySourceAnswered(sources) {
    return Object.keys(sources).every(function (id) {
      return sources[id].available;
    });
  }

  function progressDelay() {
    return Math.min(
      PROGRESS_INTERVAL_MS * Math.pow(2, progressFailures), PROGRESS_LONGEST_INTERVAL_MS
    );
  }

  function pollProgress() {
    progressTimer = null;
    if (document.hidden || !progressNodes().length) {
      return; // a background tab, or a closed dialog, must not keep asking Radarr
    }
    window
      .fetch(PROGRESS_URL, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        return response.json();
      })
      .then(function (answer) {
        progressFailures = everySourceAnswered(answer.sources) ? 0 : progressFailures + 1;
        if (applyProgress(answer) > 0) {
          scheduleProgress();
        }
      })
      .catch(function () {
        /* A failed poll says nothing about the download — leave the last figures alone
         * and try again, less often. */
        progressFailures += 1;
        scheduleProgress();
      });
  }

  function scheduleProgress() {
    if (progressTimer === null && !document.hidden && progressNodes().length) {
      progressTimer = window.setTimeout(pollProgress, progressDelay());
    }
  }

  function stopProgress() {
    if (progressTimer !== null) {
      window.clearTimeout(progressTimer);
      progressTimer = null;
    }
  }

  if (PROGRESS_URL && window.fetch) {
    scheduleProgress();
    // Indicators arrive after load too — the movie dialog, the search results — and whichever
    // poll finds none left is the last, so closing a dialog needs no handling of its own.
    new MutationObserver(scheduleProgress).observe(document.body, { childList: true, subtree: true });
    document.addEventListener("visibilitychange", function () {
      stopProgress();
      if (!document.hidden) {
        pollProgress(); // catch up on what changed while the tab was away
      }
    });
  }
})();
