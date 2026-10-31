/**
 * The few DOM helpers that stand in for a framework (as in Reolink Stamina's panel).
 *
 * Everything the panel shows that came from the server goes in as text, never as markup:
 * `el` refuses an `innerHTML` (or `outerHTML`) prop and any `on*` value that is not a
 * function, so a string can never become markup or an inline handler.
 */

const MARKUP = new Set(["innerHTML", "outerHTML", "html", "srcdoc"]);

/** Create an element with attributes, listeners and children. */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (MARKUP.has(key)) throw new TypeError(`el: "${key}" is not allowed`);
    if (key.toLowerCase().startsWith("on")) {
      if (typeof value !== "function") throw new TypeError(`el: "${key}" must be a function`);
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key === "props") {
      for (const name of Object.keys(value)) {
        if (MARKUP.has(name) || name.toLowerCase().startsWith("on")) {
          throw new TypeError(`el: prop "${name}" is not allowed`);
        }
      }
      Object.assign(node, value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Append a nested array of children, skipping empties; strings become text. */
export function append(parent, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

/** Replace all children of a node. */
export function replace(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

/** Attach stylesheet text to a shadow root. */
export function adoptStyles(root, cssText) {
  try {
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(cssText);
    root.adoptedStyleSheets = [...root.adoptedStyleSheets, sheet];
  } catch {
    root.append(el("style", { text: cssText }));
  }
}

/** An icon, using Home Assistant's own <ha-icon> when it is there. */
export function icon(name, extraClass = "") {
  const cls = `icon ${extraClass}`.trim();
  if (customElements.get("ha-icon")) return el("ha-icon", { icon: name, class: cls, "aria-hidden": "true" });
  return el("span", { class: `${cls} icon--missing`, "aria-hidden": "true" });
}
