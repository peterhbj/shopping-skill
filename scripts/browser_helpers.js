/**
 * Andorinha Browser Helpers v2
 *
 * Identificação de botões por aria-label/texto (não por posição).
 * Extração de unit_count, weight_g, volume_ml, price_per_base_unit.
 * Injetar via Playwright page.add_init_script().
 */

(() => {
  if (window.__ANDORINHA_HELPERS_V5__) return;
  window.__ANDORINHA_HELPERS_V5__ = true;
  window.__ANDORINHA_HELPERS_V4__ = true;
  window.__ANDORINHA_HELPERS_V3__ = true;
  window.__ANDORINHA_HELPERS_V2__ = true;

  function _cards() {
    const selectors = [
      '.item-product-wrapper',
      '[class*="item-product-wrapper"]',
      '[class*="item-product"]',
      '[class*="product-card"]',
      '[class*="ProductCard"]',
    ];
    for (const sel of selectors) {
      const found = document.querySelectorAll(sel);
      if (found.length) return found;
    }
    return document.querySelectorAll('.item-product-wrapper');
  }

  function _texts(el) {
    return Array.from(el.querySelectorAll('*'))
      .filter(e => e.childElementCount === 0 && e.textContent.trim())
      .map(e => e.textContent.trim());
  }

  function _parseUnitCount(name) {
    // Pack size first: C/24, C/16, C/2 (papel higiênico / toalha).
    // Do NOT treat "100fls" (folhas) as unit_count — that used to buy 1 pack
    // when the list asked for 2.
    let m = name.match(/(?:^|[^\w])c\/\s*(\d+)\b/i);
    if (m) return parseInt(m[1], 10);
    m = name.match(/(?:^|\s)(?:leve|lv\.?)\s*(\d+)/i);
    if (m) return parseInt(m[1], 10);
    m = name.match(
      /(?:^|\s)(\d+)\s*(?:un\b|unid|rolos?|sachês?|saches?|sachetes?|cápsulas?|capsulas?|caps?\b|tab\.?\b)/i
    );
    return m ? parseInt(m[1], 10) : null;
  }

  function _parseWeightGrams(name) {
    let m = name.match(/(\d+(?:[.,]\d+)?)\s*kg\b/i);
    if (m) return parseFloat(m[1].replace(',', '.')) * 1000;
    m = name.match(/(\d+(?:[.,]\d+)?)\s*g\b/i);
    if (m) return parseFloat(m[1].replace(',', '.'));
    return null;
  }

  function _parseVolumeMl(name) {
    let m = name.match(/(\d+(?:[.,]\d+)?)\s*(?:lt?|litros?)\b/i);
    if (m) return parseFloat(m[1].replace(',', '.')) * 1000;
    m = name.match(/(\d+(?:[.,]\d+)?)\s*ml\b/i);
    if (m) return parseFloat(m[1].replace(',', '.'));
    return null;
  }

  function _computePricePerBase(price, unitCount, weightG, volumeMl, isPerKg) {
    if (isPerKg) return { value: price, dim: 'kg' };
    if (volumeMl) {
      const liters = volumeMl / 1000;
      return { value: price / liters, dim: 'l' };
    }
    if (weightG) {
      const kg = weightG / 1000;
      return { value: price / kg, dim: 'kg' };
    }
    if (unitCount && unitCount > 1) {
      return { value: price / unitCount, dim: 'un' };
    }
    return { value: price, dim: 'un' };
  }

  function _labelOf(el) {
    return [
      el.getAttribute('aria-label') || '',
      el.getAttribute('title') || '',
      el.getAttribute('data-tooltip') || '',
      el.className?.toString?.() || '',
      (el.textContent || '').trim(),
    ].join(' ').toLowerCase();
  }

  function _findButtons(card) {
    // Andorinha/OSuper 2026 (confirmado por dump + screenshot):
    // - Fora do carrinho: 1 botão azul circular com SVG "+" (texto vazio)
    // - No carrinho: botão "-" | número | botão "+"  (SVGs, sem aria-label)
    // - Nunca usar o <a> do card
    const nativeButtons = Array.from(card.querySelectorAll('button'));

    const byLabel = (re) => nativeButtons.find(b => re.test(_labelOf(b)));
    const byText = (re) => nativeButtons.find(b => re.test((b.textContent || '').trim()));

    // Botões só-ícone (aspect-square / SVG / texto vazio ou +/-)
    const iconBtns = nativeButtons.filter(b => {
      const txt = (b.textContent || '').trim();
      if (txt.length > 2 && !/^[\+\-−–]$/.test(txt)) return false;
      if (b.querySelector('svg')) return true;
      if (/^[\+\-−–]$/.test(txt)) return true;
      const cls = (b.className || '').toString();
      return /aspect-square|rounded-full|btn/.test(cls) && txt.length <= 2;
    });

    let plus = byLabel(/aumentar|incrementar|plus/i) || byText(/^\s*[\+]\s*$/);
    let minus = byLabel(/diminuir|remover|minus|decrement/i) || byText(/^\s*[\-−–]\s*$/);
    let add = byLabel(/adicionar(?!\s*(à|a)\s*(lista|favoritos?))|add\s*to\s*cart|carrinho/i)
      || byText(/adicionar/i);

    // Layout real: 2+ ícones → primeiro = minus, último = plus
    if (iconBtns.length >= 2) {
      if (!minus) minus = iconBtns[0];
      if (!plus) plus = iconBtns[iconBtns.length - 1];
    } else if (iconBtns.length === 1) {
      // Um único "+" = adicionar ao carrinho
      if (!add && !plus) add = iconBtns[0];
    }

    if (!add && !plus && nativeButtons.length === 1) {
      add = nativeButtons[0];
    }

    return { add, plus, minus, all: nativeButtons };
  }

  function _clickables(card) {
    return Array.from(card.querySelectorAll('button'));
  }

  /** Debug: cola no F12 → andorinha_dump_buttons() */
  window.andorinha_dump_buttons = function (limit = 3) {
    const cards = _cards();
    return Array.from(cards).slice(0, limit).map((card, i) => {
      const els = _clickables(card);
      return {
        index: i,
        name: (card.querySelector('img') || {}).alt || '',
        clickables: els.map(el => ({
          tag: el.tagName,
          aria: el.getAttribute('aria-label'),
          title: el.getAttribute('title'),
          class: (el.className || '').toString().slice(0, 120),
          text: (el.textContent || '').trim().slice(0, 80),
          html: (el.innerHTML || '').slice(0, 120),
        })),
        resolved: (() => {
          const b = _findButtons(card);
          return {
            has_add: !!b.add,
            has_plus: !!b.plus,
            has_minus: !!b.minus,
            add_text: b.add ? _labelOf(b.add).slice(0, 80) : null,
          };
        })(),
      };
    });
  };

  function _readQtyInCard(card) {
    // NÃO pegar o primeiro dígito da árvore — o site tem "360°", preços, etc.
    const leaves = Array.from(card.querySelectorAll('*')).filter(
      e => e.childElementCount === 0 && /^\d+$/.test((e.textContent || '').trim())
    );
    const reasonable = leaves
      .map(e => parseInt(e.textContent.trim(), 10))
      .filter(n => n >= 0 && n <= 99 && n !== 360);
    if (reasonable.length) {
      const cartish = reasonable.filter(n => n >= 1 && n <= 30);
      if (cartish.length) return cartish[0];
      if (reasonable.includes(0)) return 0;
      return reasonable[0];
    }
    return 0;
  }

  window.andorinha_get_results = function () {
    const cards = _cards();
    if (!cards.length) {
      return { error: 'no_results', url: location.href };
    }
    return {
      count: cards.length,
      url: location.href,
      results: Array.from(cards).map((card, i) => {
        const img = card.querySelector('img');
        const name = img ? img.alt : (_texts(card)[0] || '');
        const texts = _texts(card);
        const priceText = texts.find(t => /^R\$\s*[\d.,]+$/.test(t)) || '';
        const price = parseFloat(
          priceText.replace('R$', '').replace(/\./g, '').replace(',', '.').trim()
        ) || 0;
        const isPerKg = texts.some(t => /R\$.*\/?\s*kg/i.test(t));
        const unit = texts.find(t => ['un', 'kg', 'g', 'l', 'ml'].includes(t)) || 'un';
        const unitCount = _parseUnitCount(name);
        const weightG = _parseWeightGrams(name);
        const volumeMl = _parseVolumeMl(name);
        const ppb = _computePricePerBase(price, unitCount, weightG, volumeMl, isPerKg);
        const btns = _findButtons(card);
        const hasMinus = !!btns.minus;
        const qty = _readQtyInCard(card);
        const inCart = hasMinus && qty > 0;
        return {
          index: i,
          name,
          name_lower: name.toLowerCase(),
          price_text: priceText,
          price_num: price,
          unit,
          unit_count: unitCount,
          weight_g: weightG,
          volume_ml: volumeMl,
          price_per_base_unit: Math.round(ppb.value * 10000) / 10000,
          price_base_dim: ppb.dim,
          price_per_kg: isPerKg,
          in_cart: inCart,
          current_qty: qty,
          button_count: btns.all.length,
          has_add: !!btns.add,
          has_plus: !!btns.plus,
          has_minus: !!btns.minus,
        };
      }),
    };
  };

  window.andorinha_get_current_qty = function (index) {
    const card = _cards()[index];
    return card ? _readQtyInCard(card) : 0;
  };

  window.andorinha_card_state = function (index) {
    const card = _cards()[index];
    if (!card) return { error: "no_card", index };
    const btns = _findButtons(card);
    return {
      index,
      qty: _readQtyInCard(card),
      has_add: !!btns.add,
      has_plus: !!btns.plus,
      has_minus: !!btns.minus,
    };
  };

  window.andorinha_click_increase = function (index) {
    const card = _cards()[index];
    if (!card) return { error: 'no_card', index };
    const btns = _findButtons(card);
    const prev = _readQtyInCard(card);
    // Prioridade: plus (já no carrinho) > add (primeira vez)
    // NUNCA clicar em minus aqui
    if (btns.plus) {
      btns.plus.click();
      return { action: 'plus', prev_qty: prev };
    }
    if (btns.add) {
      btns.add.click();
      return { action: 'add', prev_qty: prev };
    }
    // Fallback: último botão ícone (quase sempre o +)
    const icons = btns.all.filter(b => {
      const txt = (b.textContent || '').trim();
      return (txt.length <= 2 || /^[\+]/.test(txt)) && (b.querySelector('svg') || /^[\+]/.test(txt));
    });
    if (icons.length) {
      icons[icons.length - 1].click();
      return { action: 'fallback_last_icon', prev_qty: prev };
    }
    return { error: 'no_button', index, button_count: btns.all.length };
  };

window.andorinha_click_decrease = function (index) {
    const card = _cards()[index];
    if (!card) return { error: 'no_card', index };
    const btns = _findButtons(card);
    if (!btns.minus) return { error: 'no_minus', index };
    const prev = _readQtyInCard(card);
    btns.minus.click();
    return { action: 'minus', prev_qty: prev };
  };

  window.andorinha_get_cart_badge = function () {
    const selectors = [
      '#cart-btn',
      '[id*="cart-btn"]',
      '[class*="cart-qty"]',
      '[class*="cart-count"]',
      'a[href*="carrinho"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const m = (el.textContent || '').trim().match(/\d+/);
      if (m) return { qty: parseInt(m[0], 10), selector: sel };
    }
    const labeled = document.querySelector('[aria-label*="carrinho" i], [aria-label*="sacola" i]');
    if (labeled) {
      const src = labeled.getAttribute('aria-label') || labeled.textContent || '';
      const m = src.match(/\d+/);
      if (m) return { qty: parseInt(m[0], 10), selector: 'aria-label' };
    }
    return { error: 'cart_btn_not_found', qty: 0 };
  };

  window.andorinha_open_cart = function () {
    const btn = document.querySelector('#cart-btn button') || document.querySelector('#cart-btn');
    if (!btn) return { error: 'cart_btn_not_found' };
    btn.click();
    return { ok: true };
  };

  window.andorinha_get_cart_lines = function () {
    const roots = [
      document.querySelector('[class*="cart-drawer"]'),
      document.querySelector('[class*="CartDrawer"]'),
      document.querySelector('[class*="mini-cart"]'),
      document.querySelector('[class*="minicart"]'),
      document.querySelector('[id*="cart-drawer"]'),
      document.querySelector("aside"),
      document.querySelector('[role="dialog"]'),
      document.body,
    ].filter(Boolean);

    const itemSels = [
      "[class*='cart-item']",
      "[class*='CartItem']",
      "[class*='item-cart']",
      "[class*='minicart-item']",
      "[data-cart-item]",
    ];

    let nodes = [];
    for (const root of roots) {
      for (const sel of itemSels) {
        const found = root.querySelectorAll(sel);
        if (found.length) {
          nodes = Array.from(found);
          break;
        }
      }
      if (nodes.length) break;
    }

    function lineFrom(el) {
      const img = el.querySelector("img");
      let name = img && img.alt ? img.alt.trim() : "";
      const leaves = Array.from(el.querySelectorAll("*")).filter(
        (e) => e.childElementCount === 0 && (e.textContent || "").trim()
      );
      const texts = leaves.map((e) => e.textContent.trim());
      if (!name) {
        name = texts.find((t) => t.length > 8 && !/^R\$/.test(t) && !/^\d+$/.test(t)) || "";
      }
      const priceText = texts.find((t) => /^R\$\s*[\d.,]+$/.test(t)) || "";
      const price = priceText
        ? parseFloat(priceText.replace("R$", "").replace(/\./g, "").replace(",", ".").trim())
        : null;
      const qtyHit = texts.find((t) => {
        const n = parseFloat(String(t).replace(",", "."));
        return /^\d+(?:[.,]\d+)?$/.test(t) && n > 0 && n <= 99 && n !== 360;
      });
      const qty = qtyHit ? parseFloat(String(qtyHit).replace(",", ".")) : 1;
      return {
        name,
        name_lower: name.toLowerCase(),
        qty,
        price_num: Number.isFinite(price) ? price : null,
      };
    }

    const lines = nodes.map(lineFrom).filter((l) => l.name);
    return { count: lines.length, lines, url: location.href };
  };
})();
