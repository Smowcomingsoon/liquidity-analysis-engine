/**
 * La sequenza delle letture del saldo.
 *
 * Non e' una serie storica: i campioni nascono uno per apertura della
 * pagina, a distanza irregolare. Per questo l'asse x e' "l'ennesima
 * lettura" e l'etichetta lo dice — disegnarli equispaziati sotto un asse
 * temporale sarebbe una bugia sul quando.
 *
 * La soglia e' alta apposta: con due o tre punti una spezzata non descrive
 * niente ma *sembra* un andamento, e un saldo fermo disegnato come linea
 * piatta con uno scalino finale e' la bugia piu' facile da pubblicare.
 */

const MIN_SAMPLES = 5;
const HEIGHT = 44;
const PAD = 5;

export function drawSparkline(root, history, formatValue) {
  const values = history.map((point) => point.v).filter((v) => Number.isFinite(v));
  const min = Math.min(...values);
  const max = Math.max(...values);

  if (values.length < MIN_SAMPLES || max === min) {
    root.hidden = true;
    return false;
  }
  root.hidden = false;  // prima di misurare: un elemento nascosto e' largo zero

  const svg = root.querySelector("svg");
  // viewBox in pixel reali invece di unita' stirate a tutta larghezza: con
  // preserveAspectRatio="none" il punto finale diventava un'ellisse mezza
  // fuori dal bordo destro.
  const width = Math.max(160, Math.round(svg.getBoundingClientRect().width || 300));
  svg.setAttribute("viewBox", `0 0 ${width} ${HEIGHT}`);

  const x = (i) => PAD + (i / (values.length - 1)) * (width - PAD * 2);
  const y = (v) => HEIGHT - PAD - ((v - min) / (max - min)) * (HEIGHT - PAD * 2);
  const points = values.map((v, i) => [x(i), y(v)]);
  const line = points.map(([px, py], i) => `${i ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
  const [lastX, lastY] = points.at(-1);

  svg.querySelector(".spark-line").setAttribute("d", line);
  svg.querySelector(".spark-fill").setAttribute("d", `${line} L${lastX.toFixed(1)},${HEIGHT} L${points[0][0].toFixed(1)},${HEIGHT} Z`);
  const end = svg.querySelector(".spark-end");
  end.setAttribute("cx", lastX.toFixed(1));
  end.setAttribute("cy", lastY.toFixed(1));

  // Ogni estremo della scala ha la sua etichetta: senza, la linea non dice
  // di quanto si e' mossa.
  root.querySelector(".spark-lo").textContent = formatValue(min);
  root.querySelector(".spark-span").textContent = `${values.length} readings`;
  root.querySelector(".spark-hi").textContent = formatValue(max);
  return true;
}
