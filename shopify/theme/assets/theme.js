/**
 * Theme behavior layer. Vanilla JS, no build step, no framework — see
 * docs/SHOPIFY_CODING_STANDARDS.md § JavaScript. Every module here
 * progressively enhances markup that already works without it (forms
 * submit normally, links navigate normally) per
 * docs/SHOPIFY_BUILD_GUIDELINES.md § Accessibility & performance.
 */
(function () {
  'use strict';

  var liveRegion = document.getElementById('a11y-live-region');
  function announce(message) {
    if (liveRegion) liveRegion.textContent = message;
  }

  /* ---------------------------------------------------------------------
   * Focus trap helper — used by mobile nav, cart drawer, search drawer.
   * ------------------------------------------------------------------- */
  function trapFocus(container, onEscape) {
    var focusable = container.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
    if (focusable.length === 0) return function () {};
    var first = focusable[0];
    var last = focusable[focusable.length - 1];

    function handleKeydown(e) {
      if (e.key === 'Escape') {
        onEscape();
        return;
      }
      if (e.key !== 'Tab') return;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }

    container.addEventListener('keydown', handleKeydown);
    first.focus();
    return function () {
      container.removeEventListener('keydown', handleKeydown);
    };
  }

  /* ---------------------------------------------------------------------
   * Generic dialog open/close (mobile nav, cart drawer, search drawer)
   * ------------------------------------------------------------------- */
  function Dialog(root, triggerSelector, closeSelector) {
    this.root = root;
    this.releaseFocusTrap = null;
    this.lastFocused = null;

    var self = this;
    document.querySelectorAll(triggerSelector).forEach(function (trigger) {
      trigger.addEventListener('click', function () {
        self.open();
      });
    });
    root.querySelectorAll(closeSelector).forEach(function (btn) {
      btn.addEventListener('click', function () {
        self.close();
      });
    });
  }

  Dialog.prototype.open = function () {
    this.lastFocused = document.activeElement;
    this.root.hidden = false;
    this.root.parentElement.hidden = false;
    document.body.style.overflow = 'hidden';
    var self = this;
    this.releaseFocusTrap = trapFocus(this.root, function () {
      self.close();
    });
  };

  Dialog.prototype.close = function () {
    this.root.hidden = true;
    if (this.root.parentElement && this.root.parentElement.id !== 'main-content') {
      this.root.parentElement.hidden = true;
    }
    document.body.style.overflow = '';
    if (this.releaseFocusTrap) this.releaseFocusTrap();
    if (this.lastFocused) this.lastFocused.focus();
  };

  /* ---------------------------------------------------------------------
   * Mobile navigation
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-mobile-nav]').forEach(function (nav) {
    var toggle = document.querySelector('[data-mobile-nav-toggle]');
    var backdrop = document.querySelector('[data-mobile-nav-backdrop]');
    var dialog = new Dialog(nav, '[data-mobile-nav-toggle]', '[data-mobile-nav-close]');

    var originalOpen = dialog.open.bind(dialog);
    dialog.open = function () {
      originalOpen();
      if (toggle) toggle.setAttribute('aria-expanded', 'true');
      if (backdrop) backdrop.hidden = false;
    };
    var originalClose = dialog.close.bind(dialog);
    dialog.close = function () {
      originalClose();
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
      if (backdrop) backdrop.hidden = true;
    };
    if (backdrop) backdrop.addEventListener('click', function () { dialog.close(); });

    nav.querySelectorAll('[data-mobile-submenu-trigger]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var expanded = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', String(!expanded));
        var submenu = btn.nextElementSibling;
        if (submenu) submenu.hidden = expanded;
      });
    });
  });

  /* ---------------------------------------------------------------------
   * Desktop mega menu
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-mega-menu-trigger]').forEach(function (trigger) {
    var menu = document.getElementById(trigger.getAttribute('aria-controls'));
    if (!menu) return;

    function open() {
      document.querySelectorAll('[data-mega-menu]').forEach(function (m) { m.hidden = true; });
      document.querySelectorAll('[data-mega-menu-trigger]').forEach(function (t) { t.setAttribute('aria-expanded', 'false'); });
      menu.hidden = false;
      trigger.setAttribute('aria-expanded', 'true');
    }
    function close() {
      menu.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
    }

    trigger.addEventListener('click', function () {
      menu.hidden ? open() : close();
    });
    trigger.parentElement.addEventListener('mouseenter', open);
    trigger.parentElement.addEventListener('mouseleave', close);
    trigger.parentElement.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { close(); trigger.focus(); }
    });
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.header__nav-item')) {
      document.querySelectorAll('[data-mega-menu]').forEach(function (m) { m.hidden = true; });
      document.querySelectorAll('[data-mega-menu-trigger]').forEach(function (t) { t.setAttribute('aria-expanded', 'false'); });
    }
  });

  /* ---------------------------------------------------------------------
   * Cart drawer + Ajax Cart API
   * ------------------------------------------------------------------- */
  var cartDrawerRoot = document.querySelector('[data-cart-drawer]');
  var cartDialog = cartDrawerRoot
    ? new Dialog(cartDrawerRoot, '[data-cart-drawer-toggle]', '[data-cart-drawer-close]')
    : null;

  function refreshCartDrawer() {
    fetch('/?section_id=cart-drawer-fragment')
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var parser = new DOMParser();
        var doc = parser.parseFromString(html, 'text/html');
        var fresh = doc.querySelector('[data-cart-drawer]');
        if (fresh && cartDrawerRoot) {
          cartDrawerRoot.innerHTML = fresh.innerHTML;
        }
      })
      .catch(function () { /* non-fatal - drawer keeps last-known state */ });

    fetch('/cart.js')
      .then(function (r) { return r.json(); })
      .then(function (cart) {
        document.querySelectorAll('[data-cart-count]').forEach(function (el) {
          el.textContent = cart.item_count;
          el.hidden = cart.item_count === 0;
        });
      });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target.closest('[data-product-form]');
    if (!form) return;
    e.preventDefault();

    var submitBtn = form.querySelector('[data-add-to-cart]');
    var label = form.querySelector('[data-add-to-cart-text]');
    var originalLabel = label ? label.textContent : '';
    if (submitBtn) submitBtn.disabled = true;
    if (label) label.textContent = window.themeStrings.loading;

    fetch('/cart/add.js', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        id: form.querySelector('[data-variant-id-input]').value,
        quantity: form.querySelector('[data-quantity-input]') ? form.querySelector('[data-quantity-input]').value : 1
      })
    })
      .then(function (r) {
        if (!r.ok) throw new Error('add_to_cart_failed');
        return r.json();
      })
      .then(function () {
        announce(window.themeStrings.itemAdded);
        refreshCartDrawer();
        if (cartDialog) cartDialog.open();
      })
      .catch(function () {
        announce(window.themeStrings.cartUpdateError);
      })
      .finally(function () {
        if (submitBtn) submitBtn.disabled = false;
        if (label) label.textContent = originalLabel;
      });
  });

  document.addEventListener('click', function (e) {
    var removeBtn = e.target.closest('[data-cart-remove]');
    if (!removeBtn) return;
    var item = removeBtn.closest('[data-cart-item]');
    updateCartLine(item.dataset.line, 0);
  });

  document.addEventListener('click', function (e) {
    var decrease = e.target.closest('[data-cart-quantity-decrease]');
    var increase = e.target.closest('[data-cart-quantity-increase]');
    if (!decrease && !increase) return;
    var item = e.target.closest('[data-cart-item]');
    var input = item.querySelector('[data-cart-quantity-input]');
    var next = parseInt(input.value, 10) + (increase ? 1 : -1);
    if (next < 0) next = 0;
    input.value = next;
    updateCartLine(item.dataset.line, next);
  });

  function updateCartLine(line, quantity) {
    fetch('/cart/change.js', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ line: line, quantity: quantity })
    })
      .then(function (r) { return r.json(); })
      .then(function () { refreshCartDrawer(); })
      .catch(function () { announce(window.themeStrings.cartUpdateError); });
  }

  /* ---------------------------------------------------------------------
   * Search drawer + predictive search
   * ------------------------------------------------------------------- */
  var searchDrawerRoot = document.querySelector('[data-search-drawer]');
  if (searchDrawerRoot) {
    var searchDialog = new Dialog(searchDrawerRoot, '[data-search-drawer-toggle]', '[data-search-drawer-close]');
    var input = searchDrawerRoot.querySelector('[data-search-input]');
    var resultsEl = searchDrawerRoot.querySelector('[data-search-results]');
    var clearBtn = searchDrawerRoot.querySelector('[data-search-clear]');
    var debounceTimer;

    var originalSearchOpen = searchDialog.open.bind(searchDialog);
    searchDialog.open = function () {
      originalSearchOpen();
      if (input) input.focus();
    };

    if (input) {
      input.addEventListener('input', function () {
        clearTimeout(debounceTimer);
        clearBtn.hidden = input.value.length === 0;
        if (input.value.length < 2) {
          resultsEl.innerHTML = '';
          return;
        }
        debounceTimer = setTimeout(function () {
          var url = resultsEl.dataset.urlTemplate.replace('{q}', encodeURIComponent(input.value));
          fetch(url)
            .then(function (r) { return r.text(); })
            .then(function (html) {
              resultsEl.innerHTML = html;
            });
        }, 250);
      });
    }
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        input.value = '';
        resultsEl.innerHTML = '';
        clearBtn.hidden = true;
        input.focus();
      });
    }
  }

  /* ---------------------------------------------------------------------
   * Variant picker
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-variant-picker]').forEach(function (picker) {
    var dataEl = picker.querySelector('[data-variant-picker-data]');
    var variants = JSON.parse(dataEl.textContent);
    var form = picker.closest('form');
    var idInput = form ? form.querySelector('[data-variant-id-input]') : null;
    var unavailableNote = picker.querySelector('[data-variant-unavailable-note]');

    function getSelectedOptions() {
      var selected = [];
      picker.querySelectorAll('.variant-picker__option').forEach(function (fieldset) {
        var checked = fieldset.querySelector('input:checked');
        selected.push(checked ? checked.value : null);
      });
      return selected;
    }

    function findMatchingVariant(selected) {
      return variants.find(function (v) {
        var opts = [v.option1, v.option2, v.option3];
        return selected.every(function (val, i) { return val === null || opts[i] === val; });
      });
    }

    function updateUI(variant) {
      if (!variant) {
        if (unavailableNote) unavailableNote.hidden = false;
        return;
      }
      if (unavailableNote) unavailableNote.hidden = true;
      if (idInput) idInput.value = variant.id;

      var addBtn = document.querySelector('[data-add-to-cart]');
      var addLabel = document.querySelector('[data-add-to-cart-text]');
      if (addBtn) addBtn.disabled = !variant.available;
      if (addLabel) addLabel.textContent = variant.available ? window.themeStrings.addToCart : window.themeStrings.soldOut;

      var priceEl = document.querySelector('.product__info-col .price');
      if (priceEl && variant.price != null) {
        // Re-render via the cheapest correct option: refetch the section
        // markup rather than re-implement money formatting in JS, which
        // would drift from the shop's format_with_currency setting.
        fetch(window.location.pathname + '?variant=' + variant.id + '&section_id=main-product')
          .then(function (r) { return r.text(); })
          .then(function (html) {
            var doc = new DOMParser().parseFromString(html, 'text/html');
            var freshPrice = doc.querySelector('.product__info-col .price');
            if (freshPrice) priceEl.outerHTML = freshPrice.outerHTML;
          });
      }

      if (history.replaceState) {
        var url = new URL(window.location.href);
        url.searchParams.set('variant', variant.id);
        history.replaceState({}, '', url);
      }

      var media = document.querySelector('[data-media-id="' + variant.featured_media_id + '"]');
      if (media) {
        document.querySelectorAll('.product-media__slide').forEach(function (s) { s.hidden = true; });
        media.hidden = false;
      }
    }

    picker.addEventListener('change', function () {
      updateUI(findMatchingVariant(getSelectedOptions()));
    });
  });

  /* ---------------------------------------------------------------------
   * Quantity inputs (product page - cart page/drawer handled above)
   * ------------------------------------------------------------------- */
  document.addEventListener('click', function (e) {
    var dec = e.target.closest('[data-quantity-decrease]');
    var inc = e.target.closest('[data-quantity-increase]');
    if (!dec && !inc) return;
    var wrapper = e.target.closest('.quantity-input');
    var input = wrapper.querySelector('[data-quantity-input]');
    var min = parseInt(input.min || '1', 10);
    var next = parseInt(input.value, 10) + (inc ? 1 : -1);
    input.value = Math.max(min, next);
  });

  /* ---------------------------------------------------------------------
   * Product media gallery
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-product-media]').forEach(function (gallery) {
    var slides = gallery.querySelectorAll('.product-media__slide');
    var thumbs = gallery.querySelectorAll('[data-media-thumbnail]');

    function show(index) {
      slides.forEach(function (s, i) { s.hidden = i !== index; });
      thumbs.forEach(function (t, i) {
        t.classList.toggle('is-active', i === index);
        t.setAttribute('aria-selected', String(i === index));
      });
      announce('Image ' + (index + 1) + ' of ' + slides.length);
    }

    thumbs.forEach(function (thumb, i) {
      thumb.addEventListener('click', function () { show(i); });
    });

    var current = 0;
    var prevBtn = gallery.querySelector('[data-media-prev]');
    var nextBtn = gallery.querySelector('[data-media-next]');
    if (prevBtn) prevBtn.addEventListener('click', function () {
      current = (current - 1 + slides.length) % slides.length;
      show(current);
    });
    if (nextBtn) nextBtn.addEventListener('click', function () {
      current = (current + 1) % slides.length;
      show(current);
    });
  });

  /* ---------------------------------------------------------------------
   * Sticky add-to-cart bar
   * ------------------------------------------------------------------- */
  var stickyAtc = document.querySelector('[data-sticky-atc]');
  var buyButtons = document.querySelector('.product__buy-buttons');
  if (stickyAtc && buyButtons && 'IntersectionObserver' in window) {
    var observer = new IntersectionObserver(function (entries) {
      stickyAtc.hidden = entries[0].isIntersecting;
    });
    observer.observe(buyButtons);

    stickyAtc.querySelector('[data-sticky-add-to-cart]').addEventListener('click', function () {
      var mainForm = document.querySelector('[data-product-form]');
      if (mainForm) mainForm.requestSubmit();
    });
  }

  /* ---------------------------------------------------------------------
   * Facets (collection filters) — progressive enhancement over the plain
   * GET form; without JS, submitting the form still filters correctly via
   * a full page load.
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-facets-form]').forEach(function (form) {
    form.addEventListener('change', function () {
      var grid = document.querySelector('[data-collection-grid]');
      if (!grid) return;
      var params = new URLSearchParams(new FormData(form));
      var url = window.location.pathname + '?' + params.toString();
      fetch(url + '&section_id=main-collection')
        .then(function (r) { return r.text(); })
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, 'text/html');
          var freshGrid = doc.querySelector('[data-collection-grid]');
          var freshCount = doc.querySelector('[data-collection-count]');
          if (freshGrid) grid.outerHTML = freshGrid.outerHTML;
          if (freshCount) document.querySelector('[data-collection-count]').outerHTML = freshCount.outerHTML;
          history.pushState({}, '', url);
        });
    });
  });

  document.querySelectorAll('[data-facets-toggle]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var facets = document.getElementById(btn.getAttribute('aria-controls'));
      var expanded = btn.getAttribute('aria-expanded') === 'true';
      btn.setAttribute('aria-expanded', String(!expanded));
      if (facets) facets.classList.toggle('is-open', !expanded);
    });
  });

  /* ---------------------------------------------------------------------
   * Sort control
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-sort-select]').forEach(function (select) {
    select.addEventListener('change', function () {
      var url = new URL(window.location.href);
      url.searchParams.set('sort_by', select.value);
      window.location.href = url.toString();
    });
  });

  /* ---------------------------------------------------------------------
   * Announcement bar rotation + dismiss
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-announcement-rotate]').forEach(function (track) {
    var items = track.querySelectorAll('.announcement-bar__item');
    if (items.length < 2) return;
    var index = 0;
    setInterval(function () {
      items[index].hidden = true;
      index = (index + 1) % items.length;
      items[index].hidden = false;
    }, 5000);
  });

  var dismissBtn = document.querySelector('[data-announcement-dismiss]');
  if (dismissBtn) {
    var bar = dismissBtn.closest('.announcement-bar');
    if (sessionStorage.getItem('announcementDismissed') === 'true' && bar) {
      bar.hidden = true;
    }
    dismissBtn.addEventListener('click', function () {
      if (bar) bar.hidden = true;
      sessionStorage.setItem('announcementDismissed', 'true');
    });
  }

  /* ---------------------------------------------------------------------
   * Recently viewed (localStorage)
   * ------------------------------------------------------------------- */
  (function recentlyViewed() {
    var STORAGE_KEY = 'wbh:recently-viewed';
    var MAX_ITEMS = 8;

    var container = document.querySelector('[data-recently-viewed]');
    if (!container) return;

    var currentHandle = container.dataset.currentHandle;
    var stored = [];
    try {
      stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    } catch (e) {
      stored = [];
    }

    var toRender = stored.filter(function (h) { return h !== currentHandle; }).slice(0, MAX_ITEMS);

    if (toRender.length > 0) {
      var target = container.querySelector('[data-recently-viewed-target]');
      Promise.all(
        toRender.map(function (handle) {
          return fetch('/products/' + handle + '?section_id=product-card-fragment')
            .then(function (r) { return (r.ok ? r.text() : ''); });
        })
      ).then(function (htmlFragments) {
        var html = htmlFragments.filter(Boolean).join('');
        if (html) {
          target.innerHTML = html;
          container.hidden = false;
        }
      });
    }

    if (currentHandle) {
      stored = [currentHandle].concat(stored.filter(function (h) { return h !== currentHandle; })).slice(0, MAX_ITEMS);
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
      } catch (e) { /* localStorage unavailable - recently viewed silently no-ops */ }
    }
  })();

  /* ---------------------------------------------------------------------
   * Clear filters
   * ------------------------------------------------------------------- */
  document.querySelectorAll('[data-clear-filters]').forEach(function (btn) {
    if (btn.tagName === 'BUTTON') {
      btn.addEventListener('click', function () {
        window.location.href = window.location.pathname;
      });
    }
  });
})();
