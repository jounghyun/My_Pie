// 살 때·팔 때 전략 고르기 (코인·주식 화면 공용). 서버의 mypie/strategies/combo.py와 같은 규칙:
//   한쪽에 전략 하나(A), 둘 다(A 그리고 B), 하나라도(A 또는 B). 살 때·팔 때가 같은 전략 하나면 예전과 같다.
// 이 파일은 화면 쪽 도우미만 있고, 페이지의 el()을 쓴다.
const RULE_OP_TEXT = { and: '그리고', or: '또는' };

function normalizeRule(r) {
  const main = (r.buy && r.buy[0]) || r.strategy;
  const side = (list) => { const l = (list && list.length ? list : [main]).slice(0, 2); return l[1] === l[0] ? [l[0]] : l; };
  const buy = side(r.buy), sell = side(r.sell);
  const op = (v, names) => (v === 'or' && names.length === 2 ? 'or' : 'and');  // 전략 하나면 '그리고'로 맞춘다 (서버와 같음)
  return { strategy: main, buy, buy_op: op(r.buy_op, buy), sell, sell_op: op(r.sell_op, sell) };
}
function isSimpleRule(r) { return r.buy.length === 1 && r.sell.length === 1 && r.buy[0] === r.sell[0]; }
function sameRule(a, b) { return JSON.stringify(normalizeRule(a)) === JSON.stringify(normalizeRule(b)); }
function ruleText(r, labelOf) {
  r = normalizeRule(r);
  if (isSimpleRule(r)) return labelOf(r.buy[0]);
  const side = (names, op) => names.map(labelOf).join(` ${RULE_OP_TEXT[op]} `);
  return `살 때 ${side(r.buy, r.buy_op)} · 팔 때 ${side(r.sell, r.sell_op)}`;
}
function ruleQuery(r) {  // 시뮬레이션 요청에 붙이는 값
  r = normalizeRule(r);
  return `strategy=${r.buy[0]}` + (isSimpleRule(r) ? '' :
    `&buy=${r.buy.join(',')}&buy_op=${r.buy_op}&sell=${r.sell.join(',')}&sell_op=${r.sell_op}`);
}

// rule을 고치는 화면 조각. options(): [{value, text}], onChange(rule)는 바뀔 때마다 불린다
function ruleBuilder(rule, options, onChange) {
  const box = el('div', { class: 'rule' });
  const changed = () => { rule.strategy = rule.buy[0]; draw(); onChange(rule); };
  const draw = () => {
    box.innerHTML = '';
    const opts = options();
    for (const side of ['buy', 'sell']) {
      const row = el('div', { class: 'rule-row' }, el('span', { class: 'rule-tag ' + side, text: side === 'buy' ? '살 때' : '팔 때' }));
      rule[side].forEach((name, i) => {
        if (i === 1) {
          row.append(el('span', { class: 'rule-break' }));  // 폰에서는 두 번째 전략을 아랫줄로
          const seg = el('span', { class: 'seg rule-op' });
          for (const op of ['and', 'or']) {
            const b = el('button', { type: 'button', 'aria-pressed': rule[side + '_op'] === op, text: RULE_OP_TEXT[op],
                                     title: op === 'and' ? '둘 다 신호가 나야' : '하나라도 신호가 나면' });
            b.onclick = () => { rule[side + '_op'] = op; changed(); };
            seg.append(b);
          }
          row.append(seg);
        }
        const sel = el('select', { 'aria-label': `${side === 'buy' ? '살 때' : '팔 때'} 전략 ${i + 1}` });
        for (const o of opts) sel.append(el('option', { value: o.value, text: o.text }));
        sel.value = name;
        sel.onchange = () => {
          const old = rule[side][i];
          rule[side][i] = sel.value;
          // 팔 때가 살 때와 같은 전략 하나였으면 같이 바꾼다 (기본은 살 때·팔 때 같은 전략)
          if (side === 'buy' && i === 0 && rule.sell.length === 1 && rule.sell[0] === old) rule.sell[0] = sel.value;
          changed();
        };
        row.append(sel);
      });
      const more = rule[side].length === 1;
      const btn = el('button', { type: 'button', class: 'ghost small', text: more ? '＋ 하나 더' : '✕',
                                 title: more ? '두 전략을 같이 쓰기' : '두 번째 전략 빼기' });
      btn.onclick = () => {
        if (more) rule[side].push((opts.find(o => o.value !== rule[side][0]) || opts[0]).value);
        else { rule[side].pop(); rule[side + '_op'] = 'and'; }
        changed();
      };
      row.append(btn);
      box.append(row);
    }
  };
  draw();
  return box;
}
// 손절선 고르기 (서버 mypie/stoploss.py): 켜면 '산 값보다 N% 떨어지면 팔기'. onChange(%), 끄면 0
function stopField(pct, onChange) {
  const box = el('div', { class: 'stopline', title: '산 값보다 이만큼 떨어지면 전략과 상관없이 팔아요. 판 뒤에는 매수 신호가 새로 날 때 다시 사요' });
  const on = el('input', { type: 'checkbox', 'aria-label': '손절선 쓰기' });
  const num = el('input', { type: 'number', min: 1, max: 50, step: 0.5, inputmode: 'decimal', 'aria-label': '손절선 %' });
  on.checked = pct > 0;
  num.value = pct > 0 ? pct : 10;
  num.disabled = !on.checked;
  const fire = () => {
    num.disabled = !on.checked;
    onChange(on.checked ? Math.min(50, Math.max(1, Number(num.value) || 10)) : 0);
  };
  on.onchange = fire;
  num.oninput = fire;
  box.set = (p) => {  // 밖에서 값 바꾸기 (입력 중이면 건드리지 않는다)
    if (document.activeElement === num) return;
    on.checked = p > 0;
    if (p > 0) num.value = p;
    num.disabled = !on.checked;
  };
  box.append(el('label', { class: 'toggle' }, on, el('span', { class: 'rule-tag stop', text: '손절선' })),
             el('span', { text: '산 값보다 −' }), num, el('span', { text: '% 떨어지면 팔기' }));
  return box;
}
function stopText(pct) { return pct > 0 ? `손절선 −${pct}%` : ''; }

function ruleFields(r) {  // 저장할 값: 간단한 규칙(같은 전략 하나)이면 예전처럼 전략 이름만 (서버 rule_fields와 같음)
  r = normalizeRule(r);
  return isSimpleRule(r) ? { strategy: r.strategy }
    : { strategy: r.strategy, buy: r.buy, buy_op: r.buy_op, sell: r.sell, sell_op: r.sell_op };
}
