// Iris Macro Widget — Nutracheck-style "remaining" on your home screen.
//
// Setup (once):
//   1. Install the free "Scriptable" app from the App Store.
//   2. Scriptable → + (new script) → paste this whole file.
//   3. Set TOKEN below to the value Iris gave you.
//   4. Long-press home screen → add a "Scriptable" widget (Medium is best)
//      → edit it → Script = this one, "When Interacting: Run Script" off.
//
// It shows a calorie ring (kcal left) plus Protein / Carbs / Fat bars with
// grams remaining, refreshed through the day. Small size shows just the ring.

const ENDPOINT = "https://iris-familiar.fly.dev/macros/today";
const TOKEN = "PASTE_TOKEN_HERE"; // <-- replace with the token from Iris

const COL = {
  bg: new Color("#111214"),
  track: new Color("#2c2c2e"),
  text: Color.white(),
  sub: new Color("#9a9aa0"),
  cal: new Color("#30d158"),   // green
  protein: new Color("#ff453a"), // red
  carbs: new Color("#ff9f0a"),   // amber
  fat: new Color("#bf5af2"),     // purple
  over: new Color("#ff453a"),
};

async function getData() {
  const req = new Request(`${ENDPOINT}?token=${encodeURIComponent(TOKEN)}`);
  req.timeoutInterval = 15;
  return await req.loadJSON();
}

function ringImage(percent, size, lineWidth, fillColor) {
  const ctx = new DrawContext();
  ctx.size = new Size(size, size);
  ctx.opaque = false;
  ctx.respectScreenScale = true;
  const center = size / 2;
  const radius = center - lineWidth / 2 - 1;
  const dot = (deg, color) => {
    const rad = ((deg - 90) * Math.PI) / 180;
    const x = center + radius * Math.cos(rad) - lineWidth / 2;
    const y = center + radius * Math.sin(rad) - lineWidth / 2;
    ctx.setFillColor(color);
    ctx.fillEllipse(new Rect(x, y, lineWidth, lineWidth));
  };
  for (let d = 0; d < 360; d += 2) dot(d, COL.track);
  const end = Math.max(0, Math.min(1, percent)) * 360;
  for (let d = 0; d <= end; d += 2) dot(d, fillColor);
  return ctx.getImage();
}

function addCalorieRing(container, data, size, lineWidth) {
  const target = data.targets.calories || 0;
  const consumed = data.consumed.calories || 0;
  const remaining = data.remaining.calories;
  const pct = target > 0 ? consumed / target : 0;
  const over = remaining < 0;

  const box = container.addStack();
  box.size = new Size(size, size);
  box.backgroundImage = ringImage(pct, size, lineWidth, over ? COL.over : COL.cal);
  box.layoutVertically();
  box.addSpacer();

  const valRow = box.addStack();
  valRow.addSpacer();
  const val = valRow.addText(`${Math.round(Math.abs(remaining))}`);
  val.font = Font.boldSystemFont(size > 130 ? 30 : 24);
  val.textColor = over ? COL.over : COL.text;
  valRow.addSpacer();

  const lblRow = box.addStack();
  lblRow.addSpacer();
  const lbl = lblRow.addText(over ? "kcal over" : "kcal left");
  lbl.font = Font.systemFont(10);
  lbl.textColor = COL.sub;
  lblRow.addSpacer();

  box.addSpacer();
}

function addBar(col, label, consumed, target, color) {
  const row = col.addStack();
  row.layoutVertically();

  const head = row.addStack();
  const l = head.addText(label);
  l.font = Font.mediumSystemFont(11);
  l.textColor = COL.text;
  head.addSpacer();
  const rem = Math.round((target || 0) - (consumed || 0));
  const r = head.addText(`${rem}g left`);
  r.font = Font.systemFont(11);
  r.textColor = rem < 0 ? COL.over : COL.sub;

  row.addSpacer(4);

  const W = 122, H = 7;
  const track = row.addStack();
  track.size = new Size(W, H);
  track.cornerRadius = H / 2;
  track.backgroundColor = COL.track;
  const pct = target > 0 ? Math.min(1, consumed / target) : 0;
  const fill = track.addStack();
  fill.size = new Size(Math.max(2, W * pct), H);
  fill.cornerRadius = H / 2;
  fill.backgroundColor = color;
}

function errorWidget(msg) {
  const w = new ListWidget();
  w.backgroundColor = COL.bg;
  const t = w.addText("Macros unavailable");
  t.font = Font.boldSystemFont(14);
  t.textColor = COL.text;
  w.addSpacer(4);
  const s = w.addText(msg);
  s.font = Font.systemFont(10);
  s.textColor = COL.sub;
  return w;
}

async function main() {
  let data;
  try {
    data = await getData();
    if (!data || !data.targets) throw new Error("bad response");
  } catch (e) {
    const w = errorWidget(String(e).slice(0, 80));
    Script.setWidget(w);
    Script.complete();
    return;
  }

  const family = config.widgetFamily || "medium";
  const w = new ListWidget();
  w.backgroundColor = COL.bg;
  w.setPadding(12, 14, 12, 14);

  if (family === "small") {
    const wrap = w.addStack();
    wrap.addSpacer();
    addCalorieRing(wrap, data, 118, 12);
    wrap.addSpacer();
  } else {
    const main = w.addStack();
    main.centerAlignContent();
    addCalorieRing(main, data, 120, 13);
    main.addSpacer(16);
    const right = main.addStack();
    right.layoutVertically();
    addBar(right, "Protein", data.consumed.protein, data.targets.protein, COL.protein);
    right.addSpacer(9);
    addBar(right, "Carbs", data.consumed.carbs, data.targets.carbs, COL.carbs);
    right.addSpacer(9);
    addBar(right, "Fat", data.consumed.fat, data.targets.fat, COL.fat);
  }

  Script.setWidget(w);
  Script.complete();
}

await main();
