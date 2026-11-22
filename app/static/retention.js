import {el, entries, put, q, stamp, table} from './paper.js';

const VERDICT_TEXT = {LOCK: '锁定', KEEP: '保留', DOWNGRADE: '降级'};
const REASON_TEXT = {
  LOAN_CABINET: '借展柜原档整体留存',
  SUSTAINED_EPISODE: '持续异常段审计抽查，原读数锁定',
  UNREPORTED_SUSTAINED: '持续段无核销工单，证据锁定',
  DEPLOYMENT_CONFLICT: '部署重叠待查，原读数锁定',
  READING_CONFLICT: '矛盾读数隔离，原档锁定备查',
  TRANSIENT_EPISODE: '瞬时异常段抽查原读数，保留关联原档',
  TICKET_REVIEW: '工单勾稽原读数仍会抽查，保留关联原档',
  EVIDENCE_REFERENCED: '异常段或工单引用原读数，按台账保留',
  NO_OPEN_EVIDENCE: '无异常段、工单与冲突，可降为摘要',
  ALREADY_DOWNGRADED: '先前结转已降级，保持摘要不重复释放',
};

function bytes(value) {
  return `${value.toLocaleString('zh-CN')} B`;
}

function verdictBadge(code) {
  return el('span', VERDICT_TEXT[code] ?? code, `state ${code.toLowerCase()}`);
}

function renderBatches(batches, onPick, activeId) {
  const target = q('#batch-list');
  if (!batches.length) {
    target.replaceChildren(el('p', '尚无结转批次。', 'empty'));
    return;
  }
  target.replaceChildren(...batches.map((batch) => {
    const line = el('button', `${batch.season} · ${batch.totals.shifts} 班`, 'history-item');
    line.type = 'button';
    if (batch.id === activeId) line.setAttribute('aria-current', 'true');
    line.addEventListener('click', () => onPick(batch.id));
    return line;
  }));
}

function renderBatch(batch) {
  const conclusions = batch.conclusions;
  table(q('#conclusions'), conclusions, [
    ['班次', (item) => item.shift, 'mono'],
    ['结论', (item) => verdictBadge(item.verdict)],
    ['首要依据', (item) => REASON_TEXT[item.reasons[0].code] ?? item.reasons[0].code],
    ['原档', (item) => bytes(item.original_bytes), 'num'],
    ['摘要', (item) => bytes(item.summary_bytes), 'num'],
    ['释放量', (item) => bytes(item.released_bytes), 'num'],
    ['保留原行', (item) => `${item.retained_row_count}/${item.total_row_count}`, 'num'],
  ], '本批次无班次结论。');

  const refs = conclusions.reduce((sum, item) => sum + Object.keys(item.referenced).length, 0);
  q('#retention-meta').replaceChildren(
    entries([
      ['结转季', batch.season],
      ['批次号', batch.id.slice(0, 16)],
      ['生成时刻', stamp(batch.created)],
      ['锁定', batch.verdict_counts.LOCK],
      ['保留', batch.verdict_counts.KEEP],
      ['降级', batch.verdict_counts.DOWNGRADE],
      ['结转前库存', bytes(batch.totals.stored_bytes_before)],
      ['结转后库存', bytes(batch.totals.stored_bytes_after)],
      ['累计释放', bytes(batch.totals.released_bytes)],
      ['共享证据引用', `${refs} 行`],
    ]),
    el('p', '逐班依据明细：', 'rule-title'),
    ...conclusions.flatMap((item) => item.reasons.map((reason) =>
      el('p', `${item.shift} · ${REASON_TEXT[reason.code] ?? reason.detail}`, 'proof-line'))),
  );
}

export async function retention({get}) {
  const form = q('#carry-form');
  const seasonInput = q('#season');
  const carryButton = q('#carry');

  async function loadBatches(activeId) {
    const batches = await get('/api/retention/batches');
    renderBatches(batches, (id) => openBatch(id), activeId);
    if (activeId) renderBatch(batches.find((item) => item.id === activeId));
    else if (batches.length) openBatch(batches[0].id);
    else q('#conclusions').replaceChildren(el('p', '填写结转季并生成结论。', 'empty'));
  }

  async function openBatch(id) {
    const batch = await get(`/api/retention/batches/${id}`);
    renderBatch(batch);
    const batches = await get('/api/retention/batches');
    renderBatches(batches, openBatch, id);
  }

  carryButton.addEventListener('click', async () => {
    carryButton.disabled = true;
    try {
      const response = await fetch('/api/retention/carry-over', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({season: seasonInput.value.trim()}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data?.message ?? `无法读取 HTTP ${response.status}`);
      renderBatch(data.batch);
      await loadBatches(data.batch.id);
      put('#statusbar', data.reused ? '同次结转重跑，沿用原结论' : '降级结论已生成并入库');
    } catch (error) {
      put('#statusbar', `结转失败，已保留旧版：${error.message}`);
    } finally {
      carryButton.disabled = false;
    }
  });

  form.addEventListener('submit', (event) => event.preventDefault());
  await loadBatches(null);
}
