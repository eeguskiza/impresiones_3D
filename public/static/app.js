"use strict";

const $ = (s) => document.querySelector(s);
const euros = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR" });
const gramos = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const sinMovimiento = matchMedia("(prefers-reduced-motion: reduce)");
const DEBOUNCE = 250;
const MAX_SUBIDA = 100 * 1024 * 1024;
const NECESARIOS = /^(\[Content_Types\]\.xml|3D\/3dmodel\.model|Metadata\/(project_settings\.config|slice_info\.config|plate_\d+\.png)|Auxiliaries\/\.thumbnails\/thumbnail_middle\.png)$/;
const MALLAS = /^3D\/.+\.model$/;
const SERIES = { material: 1, maquina: 2, complejidad: 3, tiempo: 4, margen: 5 };

const estado = { config: null, analisis: null, total: 0, temporizador: 0, peticion: null, valido: false, tiempo: null, inicioManual: false };

function el(etiqueta, props = {}, hijos = []) {
  const n = Object.assign(document.createElement(etiqueta), props);
  for (const h of [].concat(hijos)) n.append(h);
  return n;
}

async function api(url, opciones = {}) {
  const r = await fetch(url, opciones);
  if (!r.ok) {
    let detalle = `Error ${r.status}`;
    try {
      const j = await r.json();
      detalle = typeof j.detail === "string" ? j.detail : "Revisa los datos introducidos.";
    } catch {}
    const e = new Error(detalle);
    e.status = r.status;
    throw e;
  }
  return r;
}

function numero(valor) {
  const v = String(valor).trim().replace(",", ".");
  if (v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : NaN;
}

function duracion(horas) {
  const total = Math.round(horas * 60);
  if (total < 60) return `${total} min`;
  return `${Math.floor(total / 60)} h ${String(total % 60).padStart(2, "0")} min`;
}

function tostada(texto) {
  const t = $("#tostada");
  t.textContent = texto;
  t.hidden = false;
  t.style.animation = "none";
  t.offsetHeight;
  t.style.animation = "";
  clearTimeout(tostada.t);
  tostada.t = setTimeout(() => (t.hidden = true), 5000);
}

function error(texto) {
  const e = $("#error");
  e.textContent = texto || "";
  e.hidden = !texto;
}

async function cargarConfig() {
  const c = await (await api("/api/config")).json();
  estado.config = c;
  document.documentElement.style.setProperty("--acento", c.acento);
  if (!c.marca.includes("[REEMPLAZAR]")) $("#nombre-marca").textContent = c.marca;
  const p = $("#pendientes");
  p.hidden = !c.pendientes.length;
  p.replaceChildren("Rellena en config.toml los campos marcados como [REEMPLAZAR]: ",
    ...c.pendientes.flatMap((k, i) => [i ? ", " : "", el("code", { textContent: k })]));
}

function leerZip(buf) {
  const v = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (v.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) return null;
  let total = v.getUint16(eocd + 10, true), p = v.getUint32(eocd + 16, true);
  if (p === 0xffffffff && v.getUint32(eocd - 20, true) === 0x07064b50) {
    const z64 = Number(v.getBigUint64(eocd - 12, true));
    total = Number(v.getBigUint64(z64 + 32, true));
    p = Number(v.getBigUint64(z64 + 48, true));
  }
  const entradas = [];
  for (let n = 0; n < total; n++) {
    if (v.getUint32(p, true) !== 0x02014b50) return null;
    const ln = v.getUint16(p + 28, true), le = v.getUint16(p + 30, true), lc = v.getUint16(p + 32, true);
    const e = { metodo: v.getUint16(p + 10, true), crc: v.getUint32(p + 16, true), comp: v.getUint32(p + 20, true),
      tam: v.getUint32(p + 24, true), local: v.getUint32(p + 42, true),
      nombre: new TextDecoder().decode(buf.subarray(p + 46, p + 46 + ln)) };
    for (let q = p + 46 + ln; q < p + 46 + ln + le; q += 4 + v.getUint16(q + 2, true)) {
      if (v.getUint16(q, true) !== 1) continue;
      let r = q + 4;
      for (const k of ["tam", "comp", "local"]) {
        if (e[k] === 0xffffffff) { e[k] = Number(v.getBigUint64(r, true)); r += 8; }
      }
    }
    entradas.push(e);
    p += 46 + ln + le + lc;
  }
  const flujo = (e) => {
    const inicio = e.local + 30 + v.getUint16(e.local + 26, true) + v.getUint16(e.local + 28, true);
    const datos = new Blob([buf.subarray(inicio, inicio + e.comp)]).stream();
    return e.metodo === 8 ? datos.pipeThrough(new DecompressionStream("deflate-raw")) : datos;
  };
  return { entradas, flujo };
}

async function contiene(lector, patrones) {
  const dec = new TextDecoder("latin1");
  let cola = "";
  for (;;) {
    const { done, value } = await lector.read();
    if (done) return false;
    const texto = cola + dec.decode(value, { stream: true });
    if (patrones.some((x) => texto.includes(x))) {
      lector.cancel();
      return true;
    }
    cola = texto.slice(-32);
  }
}

async function aligerar(fichero) {
  const zip = leerZip(new Uint8Array(await fichero.arrayBuffer()));
  if (!zip) return { cuerpo: fichero, pintado: null };
  let pintado = false;
  for (const e of zip.entradas.filter((e) => MALLAS.test(e.nombre))) {
    if (await contiene(zip.flujo(e).getReader(), ["paint_color", "mmu_segmentation"])) { pintado = true; break; }
  }
  const partes = [], central = [];
  let desplazamiento = 0;
  for (const e of zip.entradas.filter((e) => NECESARIOS.test(e.nombre))) {
    const datos = new Uint8Array(await new Response(zip.flujo(e)).arrayBuffer());
    const nombre = new TextEncoder().encode(e.nombre);
    const local = new DataView(new ArrayBuffer(30)), cen = new DataView(new ArrayBuffer(46));
    for (const [d, firma, base] of [[local, 0x04034b50, 4], [cen, 0x02014b50, 6]]) {
      d.setUint32(0, firma, true);
      d.setUint16(base, 20, true);
      d.setUint16(base + 2, 0x0800, true);
      d.setUint32(base + 10, e.crc, true);
      d.setUint32(base + 14, datos.length, true);
      d.setUint32(base + 18, datos.length, true);
      d.setUint16(base + 22, nombre.length, true);
    }
    cen.setUint16(4, 20, true);
    cen.setUint32(42, desplazamiento, true);
    partes.push(local, nombre, datos);
    central.push(cen, nombre);
    desplazamiento += 30 + nombre.length + datos.length;
  }
  const tamCentral = central.reduce((s, x) => s + x.byteLength, 0);
  const fin = new DataView(new ArrayBuffer(22));
  fin.setUint32(0, 0x06054b50, true);
  fin.setUint16(8, central.length / 2, true);
  fin.setUint16(10, central.length / 2, true);
  fin.setUint32(12, tamCentral, true);
  fin.setUint32(16, desplazamiento, true);
  return { cuerpo: new Blob([...partes, ...central, fin]), pintado };
}

async function subir(fichero) {
  if (!fichero) return;
  if (!fichero.name.toLowerCase().endsWith(".3mf")) {
    error("El fichero debe ser un .3mf de Bambu Studio.");
    return;
  }
  if (fichero.size > MAX_SUBIDA) {
    error("El fichero supera el límite de 100 MB.");
    return;
  }
  error("");
  $("#resultado").hidden = true;
  $("#soltar").classList.add("compacto");
  $("#esqueleto").hidden = false;
  try {
    const { cuerpo, pintado } = await aligerar(fichero).catch(() => ({ cuerpo: fichero, pintado: null }));
    const extra = pintado === null ? "" : `&pintado=${pintado}`;
    const r = await api(`/api/analizar?fichero=${encodeURIComponent(fichero.name)}${extra}`, {
      method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: cuerpo,
    });
    mostrar(await r.json());
  } catch (e) {
    error(e.message);
    $("#soltar").classList.toggle("compacto", Boolean(estado.analisis));
    if (estado.analisis) $("#resultado").hidden = false;
  } finally {
    $("#esqueleto").hidden = true;
    $("#entrada").value = "";
  }
}

function mostrar(a) {
  estado.analisis = a;
  estado.total = 0;
  estado.tiempo = null;
  $("#reloj").replaceChildren();
  $("#tiempo-datos").replaceChildren();
  $("#soltar").classList.add("compacto");
  $("#pieza-img").src = a.imagen ? `data:image/png;base64,${a.imagen}` : "";
  $("#pieza-img").hidden = !a.imagen;
  $("#pieza-titulo").textContent = a.titulo;
  $("#pieza-detalle").textContent = [a.disenador && `Diseño de ${a.disenador}`, a.impresora].filter(Boolean).join(" · ");
  $("#pieza-chips").replaceChildren(
    ...(a.laminado ? [el("span", { className: "chip si", textContent: "Laminado" })] : []),
    ...a.avisos.map((x) => el("span", { className: `chip ${x.tipo === "aviso" ? "no" : ""}`, textContent: x.corto, title: x.texto })),
    ...(a.placas.length > 1 ? [el("span", { className: "chip", textContent: `${a.placas.length} placas` })] : []),
    el("span", { className: "chip", textContent: a.fichero }),
  );

  const f = $("#formulario");
  f.minutos.value = String(estado.config.minutos).replace(".", ",");
  f.margen.value = String(Math.round(estado.config.margen * 1000) / 10).replace(".", ",");
  f.horas.value = "";
  f.mins.value = "";
  $("#manual").hidden = a.laminado;

  $("#filamentos").replaceChildren(...a.filamentos.map((fil) => {
    const precio = el("input", { name: `eur_kg_${fil.id}`, inputMode: "decimal",
      value: fil.eur_kg == null ? "" : String(fil.eur_kg).replace(".", ","), placeholder: "€/kg" });
    precio.setAttribute("aria-label", `€/kg del filamento ${fil.id}`);
    const g = a.laminado
      ? el("span", { className: "gramos-fijos", textContent: `${gramos.format(fil.gramos)} g` })
      : el("span", { className: "con-unidad" }, [
          Object.assign(el("input", { name: `gramos_${fil.id}`, inputMode: "decimal", placeholder: "0" }),
            { ariaLabel: `Gramos del filamento ${fil.id}` }),
          el("i", { textContent: "g" })]);
    const fila = el("div", { className: "fila-filamento" }, [
      el("span", { className: "color", title: fil.color }),
      el("div", {}, [el("strong", { textContent: fil.perfil }),
        el("small", { textContent: `${fil.nombre_color} · ${fil.tipo} · #${fil.id}` })]),
      el("span", { className: "con-unidad" }, [precio, el("i", { textContent: "€/kg" })]),
      g,
    ]);
    fila.querySelector(".color").style.background = fil.color;
    return fila;
  }));

  $("#total").textContent = "—";
  $("#desglose").replaceChildren();
  $("#previa").srcdoc = "";
  $("#resultado").hidden = false;
  requestAnimationFrame(ajustarRellenos);
  actualizar();
}

function leerDatos() {
  const a = estado.analisis;
  const f = $("#formulario");
  let ok = true;
  const leer = (input) => {
    const v = numero(input.value);
    input.classList.toggle("invalido", Number.isNaN(v));
    if (Number.isNaN(v)) ok = false;
    return Number.isNaN(v) ? null : v;
  };
  const datos = {
    proyecto: a.proyecto,
    cliente: f.cliente.value.trim(),
    minutos: leer(f.minutos) ?? 0,
    margen: (leer(f.margen) ?? 0) / 100,
    eur_kg: {},
    gramos: {},
    desglose: $("#desglose-pdf").checked,
    complejidad: $("#aplicar-complejidad").checked,
  };
  for (const fil of a.filamentos) {
    const p = leer(f[`eur_kg_${fil.id}`]);
    if (p != null) datos.eur_kg[fil.id] = p;
    if (!a.laminado) {
      const g = leer(f[`gramos_${fil.id}`]);
      if (g != null) datos.gramos[fil.id] = g;
    }
  }
  if (!a.laminado) {
    const h = leer(f.horas), m = leer(f.mins);
    datos.tiempo_min = h == null && m == null ? null : (h ?? 0) * 60 + (m ?? 0);
  }
  return ok ? datos : null;
}

function programar() {
  clearTimeout(estado.temporizador);
  estado.temporizador = setTimeout(actualizar, DEBOUNCE);
}

function pendiente(mensaje) {
  estado.valido = false;
  $(".precio").classList.add("pendiente");
  $("#precio-mensaje").textContent = mensaje;
  $("#precio-mensaje").hidden = false;
  $("#exportar").disabled = true;
  $("#marco").classList.add("cargando");
}

async function actualizar() {
  if (!estado.analisis) return;
  const datos = leerDatos();
  if (!datos) return pendiente("Hay campos con valores no válidos.");
  estado.peticion?.abort();
  const control = (estado.peticion = new AbortController());
  $("#marco").classList.add("cargando");
  try {
    const r = await (await api("/api/calcular?vista=true", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(datos), signal: control.signal,
    })).json();
    pintarPrecio(r);
    const primera = !estado.tiempo;
    estado.tiempo = { horas: r.horas, minutos: datos.minutos };
    pintarTiempo(primera);
    if (control.signal.aborted) return;
    $("#previa").srcdoc = r.html;
    estado.valido = true;
    $("#exportar").disabled = false;
  } catch (e) {
    if (e.name === "AbortError") return;
    pendiente(e.message);
  }
}

function pintarPrecio(r) {
  $(".precio").classList.remove("pendiente");
  $("#precio-mensaje").hidden = true;
  contador($("#total"), estado.total, r.precio);
  estado.total = r.precio;
  $("#resumen").textContent = `${duracion(r.horas)} · ${gramos.format(r.gramos)} g`;
  const filas = [
    ["Material", euros.format(r.material)], ["Máquina, energía y fallos", euros.format(r.maquina)],
    ...(r.ajuste ? [[`Complejidad (${porcentaje(r.ajuste_pct)})`, euros.format(r.ajuste)]] : []),
    ["Preparación y acabado", euros.format(r.mano_obra)], ["Coste", euros.format(r.coste), true],
    ["Margen y redondeo", euros.format(r.margen_redondeo)],
  ];
  $("#desglose").replaceChildren(...filas.flatMap(([k, v, separado]) => {
    const c = separado ? "separado" : "";
    return [el("dt", { className: c, textContent: k }), el("dd", { className: c, textContent: v })];
  }));
  pintarComplejidad(r);
  pintarReparto(r.reparto);
  requestAnimationFrame(ajustarRellenos);
}

function tinta(nodo) {
  const [r, g, b] = getComputedStyle(nodo).backgroundColor.match(/\d+/g).map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const luz = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return 1.05 / (luz + 0.05) >= (luz + 0.05) / 0.0638 ? "#fff" : "#1B2433";
}

function resaltable(grafico, nodo, clave) {
  const marcar = (si) => {
    grafico.classList.toggle("resaltado", si);
    grafico.querySelectorAll(`[data-clave="${clave}"]`).forEach((n) => n.classList.toggle("activo", si));
  };
  for (const [ev, si] of [["mouseenter", true], ["mouseleave", false], ["focus", true], ["blur", false]]) {
    nodo.addEventListener(ev, () => marcar(si));
  }
  nodo.dataset.clave = clave;
  return nodo;
}

function pintarReparto(partes) {
  const grafico = $("#reparto"), suma = partes.reduce((s, p) => s + p.importe, 0) || 1;
  let acumulado = 0;
  const filas = partes.map((p) => {
    const inicio = acumulado / suma;
    acumulado += p.importe;
    return { ...p, inicio, fin: acumulado / suma, cuota: `${Math.round((p.importe / suma) * 100)} %` };
  });
  grafico.querySelector(".reparto-barra").replaceChildren(...filas.map((p, i) => {
    const s = resaltable(grafico, el("div", { className: "segmento", tabIndex: 0,
      title: `${p.nombre}: ${euros.format(p.importe)} (${p.cuota})` }, el("span", { textContent: p.cuota })), p.clave);
    s.setAttribute("aria-label", s.title);
    s.style.setProperty("--c", `var(--serie-${SERIES[p.clave]})`);
    s.style.top = `calc(${p.inicio * 100}% + ${i ? 1 : 0}px)`;
    s.style.height = `calc(${(p.fin - p.inicio) * 100}% - ${(i ? 1 : 0) + (i < filas.length - 1 ? 1 : 0)}px)`;
    return s;
  }));
  grafico.querySelector(".reparto-etiquetas").replaceChildren(...filas.map((p) => {
    const e = resaltable(grafico, el("div", { className: "reparto-etiqueta", title: `${p.nombre}: ${p.cuota}` },
      [el("i"), el("span", { textContent: p.nombre }), el("b", { textContent: euros.format(p.importe) })]), p.clave);
    e.style.setProperty("--c", `var(--serie-${SERIES[p.clave]})`);
    e.dataset.centro = (p.inicio + p.fin) / 2;
    return e;
  }));
  grafico.setAttribute("aria-label",
    `Reparto del precio: ${filas.map((p) => `${p.nombre} ${euros.format(p.importe)} (${p.cuota})`).join(", ")}`);
  colocarEtiquetas();
}

function colocarEtiquetas() {
  const grafico = $("#reparto"), alto = grafico.clientHeight, minimo = 22;
  const etiquetas = [...grafico.querySelectorAll(".reparto-etiqueta")];
  const centros = etiquetas.map((e) => Number(e.dataset.centro) * alto), y = [...centros];
  for (let i = 0; i < y.length; i++) y[i] = Math.max(y[i], i ? y[i - 1] + minimo : minimo / 2);
  for (let i = y.length - 1; i >= 0; i--) y[i] = Math.min(y[i], i < y.length - 1 ? y[i + 1] - minimo : alto - minimo / 2);
  etiquetas.forEach((e, i) => (e.style.top = `${y[i]}px`));
  for (const s of grafico.querySelectorAll(".segmento")) {
    s.style.setProperty("--tinta", tinta(s));
    s.firstChild.hidden = s.clientHeight < 20;
  }
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "reparto-conectores");
  svg.setAttribute("aria-hidden", "true");
  etiquetas.forEach((e, i) => {
    const linea = document.createElementNS(svg.namespaceURI, "path");
    linea.setAttribute("d", `M0 ${centros[i]} C 12 ${centros[i]}, 10 ${y[i]}, 22 ${y[i]}`);
    linea.setAttribute("class", "conector");
    linea.dataset.clave = e.dataset.clave;
    svg.append(linea);
  });
  grafico.querySelector(".reparto-conectores")?.remove();
  grafico.append(svg);
}

function svg(etiqueta, atributos = {}, hijos = []) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", etiqueta);
  for (const [k, v] of Object.entries(atributos)) n.setAttribute(k, v);
  n.append(...[].concat(hijos));
  return n;
}

function inicioLanzamiento() {
  const [h, m] = ($("#hora-inicio").value || "").split(":").map(Number);
  const inicio = new Date();
  if (Number.isFinite(h)) inicio.setHours(h, m, 0, 0);
  return inicio.getTime();
}

function pintarTiempo(animar = false) {
  if (!estado.tiempo) return;
  if (!estado.inicioManual) {
    $("#hora-inicio").value = new Date().toTimeString().slice(0, 5);
  }
  const { horas, minutos } = estado.tiempo, impresion = horas * 60, total = impresion + minutos;
  const inicio = inicioLanzamiento(), finImpresion = inicio + impresion * 60000, fin = inicio + total * 60000;
  const reloj = (t) => new Date(t).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
  const dias = Math.round((new Date(fin).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000);
  const dia = dias === 0 ? "hoy" : dias === 1 ? "mañana"
    : new Date(fin).toLocaleDateString("es-ES", { weekday: "long" });

  const C = 110, R = 88, horaDe = (t) => { const d = new Date(t); return d.getHours() + d.getMinutes() / 60; };
  const punto = (h, r = R) => {
    const a = ((h / 24) * 360 - 90) * Math.PI / 180;
    return [C + r * Math.cos(a), C + r * Math.sin(a)];
  };
  const arco = (desde, horasArco, serie, titulo) => {
    const hueco = (2 / R) * (12 / Math.PI) / 2, largo = Math.min(horasArco, 23.99) - 2 * hueco;
    if (largo <= 0) return [];
    const [x1, y1] = punto(desde + hueco), [x2, y2] = punto(desde + hueco + largo);
    return [svg("path", { class: "arco", pathLength: 1, style: `--c: var(--serie-${serie})`,
      d: `M${x1} ${y1} A${R} ${R} 0 ${largo > 12 ? 1 : 0} 1 ${x2} ${y2}` }, svg("title", {}, titulo))];
  };
  const h0 = horaDe(inicio), restante = Math.max(0, 24 - horas);
  const marcas = Array.from({ length: 24 }, (_, h) => {
    const mayor = h % 6 === 0, [x1, y1] = punto(h, 76), [x2, y2] = punto(h, mayor ? 70 : 73);
    return svg("line", { class: `marca-hora ${mayor ? "mayor" : ""}`, x1, y1, x2, y2 });
  });
  const numeros = [0, 6, 12, 18].map((h) => {
    const [x, y] = punto(h, 61);
    return svg("text", { class: "numero-hora", x, y }, String(h));
  });
  const [ax1, ay1] = punto(horaDe(Date.now()), R - 12), [ax2, ay2] = punto(horaDe(Date.now()), R + 12);
  const extremo = (h, serie, titulo) => {
    const [x, y] = punto(h);
    return svg("circle", { class: "extremo", cx: x, cy: y, r: 5, style: `--c: var(--serie-${serie})` }, svg("title", {}, titulo));
  };
  const lienzo = $("#reloj");
  lienzo.classList.toggle("animar", animar);
  lienzo.replaceChildren(
    svg("circle", { class: "pista", cx: C, cy: C, r: R }),
    ...marcas, ...numeros,
    ...arco(h0, horas, 1, `Impresión: ${reloj(inicio)} → ${reloj(finImpresion)} (${duracion(horas)})`),
    ...(minutos > 0 && restante > 0
      ? arco(h0 + horas, Math.min(minutos / 60, restante), 2, `Acabado: ${reloj(finImpresion)} → ${reloj(fin)} (${duracion(minutos / 60)})`) : []),
    svg("line", { class: "ahora", x1: ax1, y1: ay1, x2: ax2, y2: ay2 }, svg("title", {}, `Ahora: ${reloj(Date.now())}`)),
    extremo(h0, 1, `Empieza: ${reloj(inicio)}`),
    extremo(horaDe(fin), minutos > 0 ? 2 : 1, `Lista: ${reloj(fin)}`),
    svg("text", { class: "centro-valor", x: C, y: C + 4 }, duracion(total / 60)),
    svg("text", { class: "centro-texto", x: C, y: C + 24 }, "en total"),
  );
  lienzo.setAttribute("aria-label", `Empieza ${reloj(inicio)}, imprime ${duracion(horas)} hasta ${reloj(finImpresion)}`
    + `${minutos > 0 ? `, acabado ${duracion(minutos / 60)}` : ""}, lista ${dia} a las ${reloj(fin)}`);

  const fila = (texto, valor, serie, clase = "") => {
    const dt = el("dt", { className: clase }, [...(serie ? [el("i")] : []), texto]);
    if (serie) dt.firstChild.style.setProperty("--c", `var(--serie-${serie})`);
    return [dt, el("dd", { className: clase, textContent: valor })];
  };
  $("#tiempo-datos").replaceChildren(
    ...fila(`Impresión · ${duracion(horas)}`, reloj(finImpresion), 1),
    ...(minutos > 0 ? fila(`Acabado · ${duracion(minutos / 60)}`, reloj(fin), 2) : []),
    ...fila(`Lista ${dia}`, reloj(fin), 0, "lista"),
  );
}

function ajustarRellenos() {
  for (const relleno of document.querySelectorAll("#resultado .relleno")) {
    const columna = relleno.parentElement, previo = relleno.previousElementSibling, estilo = getComputedStyle(columna);
    previo.classList.remove("estira");
    relleno.classList.remove("sin-sitio");
    if (estilo.display !== "flex") continue;
    const hueco = parseFloat(estilo.rowGap) || 0;
    const ocupado = [...columna.children].filter((c) => c !== relleno && c.offsetParent)
      .reduce((s, c) => s + c.offsetHeight + hueco, 0);
    const libre = columna.clientHeight - parseFloat(estilo.paddingTop) - parseFloat(estilo.paddingBottom) - ocupado;
    const cabe = libre >= Number(relleno.dataset.minimo);
    relleno.classList.toggle("sin-sitio", !cabe);
    previo.classList.toggle("estira", !cabe);
  }
  colocarEtiquetas();
}

function porcentaje(x) {
  return `${x > 0 ? "+" : x < 0 ? "−" : ""}${Math.abs(Math.round(x * 100))} %`;
}

function pintarComplejidad(r) {
  const { minimo, maximo } = estado.config.complejidad;
  const posicion = maximo > minimo ? (r.ajuste_pct - minimo) / (maximo - minimo) : 0.5;
  $(".complejidad").classList.toggle("apagada", !r.aplicado);
  $("#complejidad-nivel").textContent = r.nivel;
  $("#complejidad-pct").textContent = porcentaje(r.ajuste_pct);
  $("#medidor-marca").style.left = `${Math.min(1, Math.max(0, posicion)) * 100}%`;
  $("#motivos").replaceChildren(...(r.motivos.length ? r.motivos.map((m, i) => {
    const li = el("li", {}, [
      el("span", { className: `pastilla ${m.pct < 0 ? "baja" : ""}`, textContent: porcentaje(m.pct) }),
      el("span", {}, [el("b", { textContent: m.titulo }), el("small", { textContent: m.detalle })]),
    ]);
    li.style.animationDelay = `${i * 60}ms`;
    return li;
  }) : [el("li", {}, [el("span", { className: "pastilla", textContent: "0 %" }),
      el("span", {}, [el("b", { textContent: "Pieza estándar" }), el("small", { textContent: "sin ajuste" })])])]));
}

function contador(nodo, desde, hasta) {
  cancelAnimationFrame(contador.raf);
  if (sinMovimiento.matches || desde === hasta) {
    nodo.textContent = euros.format(hasta);
    return;
  }
  const inicio = performance.now(), duracionMs = 600;
  const paso = (t) => {
    const p = Math.min(1, (t - inicio) / duracionMs);
    const e = 1 - Math.pow(1 - p, 3);
    nodo.textContent = euros.format(desde + (hasta - desde) * e);
    if (p < 1) contador.raf = requestAnimationFrame(paso);
  };
  contador.raf = requestAnimationFrame(paso);
  nodo.classList.remove("pulso");
  nodo.offsetWidth;
  nodo.classList.add("pulso");
}

async function exportar() {
  const datos = leerDatos();
  if (!datos || !estado.valido) return;
  const b = $("#exportar");
  b.disabled = true;
  b.classList.add("ocupado");
  try {
    if (!estado.config.pdf_servidor) {
      const r = await (await api("/api/registrar", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(datos),
      })).json();
      await imprimir(r.html, r.nombre);
      tostada(`Presupuesto ${r.numero} registrado. En el diálogo elige «Guardar como PDF».`);
      actualizar();
      return;
    }
    const r = await api("/api/exportar", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(datos),
    });
    const disp = r.headers.get("Content-Disposition") || "";
    const nombre = decodeURIComponent((disp.match(/filename\*=UTF-8''([^;]+)/) || [])[1] || "presupuesto.pdf");
    const url = URL.createObjectURL(await r.blob());
    el("a", { href: url, download: nombre }).click();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    tostada(`Presupuesto ${r.headers.get("X-Numero")} guardado en ${decodeURIComponent(r.headers.get("X-Ruta") || nombre)}`);
    actualizar();
  } catch (e) {
    error(e.message);
  } finally {
    b.classList.remove("ocupado");
    b.disabled = !estado.valido;
  }
}

function imprimir(html, nombre) {
  return new Promise((listo) => {
    const marco = el("iframe", { title: nombre });
    marco.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";
    marco.addEventListener("load", async () => {
      const ventana = marco.contentWindow, titulo = document.title;
      await ventana.document.fonts.ready;
      document.title = nombre;
      ventana.focus();
      ventana.print();
      document.title = titulo;
      setTimeout(() => marco.remove(), 1000);
      listo();
    }, { once: true });
    marco.srcdoc = html;
    document.body.append(marco);
  });
}

async function marcarPagado(p, pagado) {
  await api(`/api/historial/${p.id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pagado }),
  });
  tostada(`${p.numero}: ${pagado ? "pagado" : "pendiente de pago"}`);
}

async function cargarHistorial() {
  const filas = await (await api("/api/historial")).json();
  $("#historial-vacio").hidden = filas.length > 0;
  $("#historial").replaceChildren(...filas.map((p, i) => {
    const casilla = el("input", { type: "checkbox", checked: p.pagado });
    casilla.setAttribute("aria-label", `Pagado ${p.numero}`);
    casilla.addEventListener("change", async () => {
      try {
        await marcarPagado(p, casilla.checked);
      } catch (e) {
        casilla.checked = !casilla.checked;
        tostada(e.message);
      }
    });
    const fila = el("tr", {}, [
      el("td", { textContent: p.numero }),
      el("td", { textContent: p.fecha.split("-").reverse().join("/") }),
      el("td", { textContent: p.cliente }),
      el("td", { textContent: p.modelo }),
      el("td", { className: "num", textContent: duracion(p.horas) }),
      el("td", { className: "num", textContent: gramos.format(p.gramos) }),
      el("td", { className: "num", textContent: euros.format(p.precio) }),
      el("td", {}, el("label", { className: "casilla" }, [casilla, el("span", { className: "interruptor" })])),
    ]);
    fila.style.setProperty("--i", i);
    return fila;
  }));
}

function cambiarVista(nombre) {
  for (const b of document.querySelectorAll(".pestana")) {
    const activa = b.dataset.vista === nombre;
    b.classList.toggle("activa", activa);
    b.setAttribute("aria-selected", activa);
  }
  $("#vista-nuevo").hidden = nombre !== "nuevo";
  $("#vista-historial").hidden = nombre !== "historial";
  if (nombre === "historial") cargarHistorial().catch((e) => tostada(e.message));
}

function iniciar() {
  const soltar = $("#soltar");
  let profundidad = 0;
  const arrastrar = (si) => { soltar.classList.toggle("encima", si); document.body.classList.toggle("arrastrando", si); };
  addEventListener("dragenter", (e) => { e.preventDefault(); profundidad++; arrastrar(true); });
  addEventListener("dragleave", () => { if (--profundidad <= 0) { profundidad = 0; arrastrar(false); } });
  addEventListener("dragover", (e) => e.preventDefault());
  addEventListener("drop", (e) => {
    e.preventDefault();
    profundidad = 0;
    arrastrar(false);
    cambiarVista("nuevo");
    subir(e.dataTransfer.files[0]);
  });
  $("#entrada").addEventListener("change", (e) => subir(e.target.files[0]));
  $("#cambiar").addEventListener("click", () => $("#entrada").click());
  $("#formulario").addEventListener("input", programar);
  $("#formulario").addEventListener("submit", (e) => e.preventDefault());
  $("#desglose-pdf").addEventListener("change", programar);
  $("#exportar").addEventListener("click", exportar);
  $("#aplicar-complejidad").addEventListener("change", programar);
  setInterval(() => pintarTiempo(), 60000);
  $("#hora-inicio").addEventListener("input", (e) => {
    estado.inicioManual = Boolean(e.target.value);
    pintarTiempo();
  });
  new ResizeObserver(() => ajustarRellenos()).observe($("#resultado"));
  for (const b of document.querySelectorAll(".pestana")) b.addEventListener("click", () => cambiarVista(b.dataset.vista));

  const marco = $("#marco"), previa = $("#previa");
  new ResizeObserver(() => {
    const s = Math.min(marco.clientWidth / 794, marco.clientHeight / 1123);
    previa.style.transform = `translateX(${(marco.clientWidth - 794 * s) / 2}px) scale(${s})`;
  }).observe(marco);
  previa.addEventListener("load", () => marco.classList.remove("cargando"));

  cargarConfig().catch((e) => error(`No se pudo leer la configuración: ${e.message}`));
}

iniciar();
