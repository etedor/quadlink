// shared by the pages. every glyph: Lucide 1.61.0 (ISC, lucide.dev), on its 24-unit 2px stroke grid;
// the CSS around each one sets its size
const ICONS = {
  quad: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
  explore: '<circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z"/>',
  filter: '<path d="M10 20a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7a2 2 0 0 1 .517-1.341L21.74 4.67A1 1 0 0 0 21 3H3a1 1 0 0 0-.742 1.67l7.225 7.989A2 2 0 0 1 10 14z"/>',
  sort: '<path d="m3 16 4 4 4-4"/><path d="M7 20V4"/><path d="m21 8-4-4-4 4"/><path d="M17 4v16"/>',
  reset: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  sound: '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/><path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  'lock-open': '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
  settings: '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
};
function icon(name) {
  const t = document.createElement('template');
  t.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + '</svg>';
  return t.content.firstChild;
}
document.querySelectorAll('[data-icon]').forEach(b => b.prepend(icon(b.dataset.icon)));

function el(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function option(type, name, value, text, checked, onChange) {
  const label = document.createElement('label');
  const input = document.createElement('input');
  input.type = type;
  input.name = name;
  input.value = value;
  input.checked = checked;
  input.addEventListener('change', onChange);
  const span = document.createElement('span');
  span.className = 'truncate';
  span.textContent = text;
  label.title = text;
  label.append(input, span);
  return label;
}

// a checkbox group's value is a list, so order does not count
const same = (a, b) => Array.isArray(a) ? [...a].sort().join('\n') === [...b].sort().join('\n') : a === b;

function edge(pop) {
  pop.classList.toggle('more', pop.scrollTop + pop.clientHeight < pop.scrollHeight - 1);
}

let openMenu = null;
function closeMenu(refocus = false) {
  if (!openMenu) return;
  openMenu.pop.hidden = true;
  openMenu.button.setAttribute('aria-expanded', 'false');
  if (refocus) openMenu.button.focus();
  openMenu = null;
}
document.addEventListener('click', e => { if (!e.target.closest('.pop, .tools > button')) closeMenu(); });

// a menu hangs from its button's tools row: a head (title, reset), then a fieldset per group.
// a group's choices() runs on every opening, so lists drawn from live data stay current.
// the button's dot shows while any group differs from its fallback; a reset with nothing
// changed does nothing, so it never reloads a listing
function menu({ button, title, reset, apply, groups }) {
  const pop = el('div', 'pop');
  pop.hidden = true;
  const head = el('div', 'pop-head');
  const label = el('span', '', title);
  label.setAttribute('aria-hidden', 'true');
  const undo = el('button', 'btn icon reset');
  undo.type = 'button';
  undo.title = 'Reset';
  undo.setAttribute('aria-label', reset);
  undo.append(icon('reset'));
  head.append(label, undo);
  const body = el('div', 'pop-body');
  pop.append(head, body);
  button.closest('.tools').append(pop);
  const changed = () => groups.some(g => !same(g.get(), g.fallback));
  const chose = () => { apply(); button.classList.toggle('changed', changed()); };
  const fill = () => body.replaceChildren(...groups.map(g => {
    const field = el('fieldset', '');
    field.append(el('legend', g.legend ? '' : 'sr-only', g.legend || title));
    field.append(...g.choices().map(([value, text]) => {
      const radio = g.type === 'radio';
      return option(g.type, g.name, value, text, radio ? g.get() === value : g.get().includes(value), e => {
        g.set(radio ? value : e.target.checked ? [...g.get(), value] : g.get().filter(x => x !== value));
        chose();
      });
    }));
    return field;
  }));
  undo.addEventListener('click', () => {
    if (!changed()) return;
    groups.forEach(g => g.set(Array.isArray(g.fallback) ? [...g.fallback] : g.fallback));
    chose();
    fill();
  });
  const m = { button, pop };
  button.addEventListener('click', () => {
    const open = openMenu !== m;
    closeMenu(!open);
    if (!open) return;
    fill();
    pop.hidden = false;
    pop.scrollTop = 0;
    button.setAttribute('aria-expanded', 'true');
    openMenu = m;
    (pop.querySelector('input:checked') || pop.querySelector('input'))?.focus();
    edge(pop);
  });
  pop.addEventListener('scroll', () => edge(pop));
  button.classList.toggle('changed', changed());
}
