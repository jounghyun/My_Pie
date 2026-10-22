// 라즈베리파이 상태 카드 (코인·주식 화면 공용): <section class="card" id="system"></section> 안을 채우고 15초마다 새로 고친다
(() => {
  const box = document.getElementById('system');
  if (!box) return;
  box.innerHTML = `
    <h2><span class="ico">🍓</span>라즈베리파이 <small id="sysSub"></small></h2>
    <div class="sys">
      <div class="tile"><div class="label">CPU 온도</div><div class="v num" id="sysTemp">-</div><div class="meter"><i id="sysTempBar"></i></div></div>
      <div class="tile"><div class="label">CPU 사용</div><div class="v num" id="sysLoad">-</div><div class="meter"><i id="sysLoadBar"></i></div></div>
      <div class="tile"><div class="label">메모리</div><div class="v num" id="sysMem">-</div><div class="meter"><i id="sysMemBar"></i></div></div>
      <div class="tile"><div class="label">저장공간</div><div class="v num" id="sysDisk">-</div><div class="meter"><i id="sysDiskBar"></i></div></div>
      <div class="tile power" title="파이 3B에는 전력 센서가 없어서 CPU 사용량으로 어림한 값이에요"><div class="label">전력 (추정)</div>
        <div class="v num" id="sysPower">-</div><div class="sub" id="sysPowerSub"></div><div class="meter"><i id="sysPowerBar"></i></div></div>
    </div>
    <div class="desc" id="sysFoot"></div>`;
  const $ = (id) => document.getElementById(id);

  function meter(id, ratio, warnAt = .75, badAt = .9) {
    const bar = $(id);
    bar.style.width = Math.max(2, Math.min(100, ratio * 100)) + '%';
    bar.style.background = ratio >= badAt ? 'var(--bad)' : ratio >= warnAt ? 'var(--warn)' : 'var(--ok)';
  }

  function render(x) {
    if (x.temp_c != null) { $('sysTemp').textContent = x.temp_c.toFixed(1) + '°C'; meter('sysTempBar', x.temp_c / 85, 60 / 85, 75 / 85); }
    if (x.load != null) { const r = x.load / (x.cpus || 1); $('sysLoad').textContent = Math.round(r * 100) + '%'; meter('sysLoadBar', r); }
    if (x.mem_total_mb) { $('sysMem').textContent = `${x.mem_used_mb} / ${x.mem_total_mb}MB`; meter('sysMemBar', x.mem_used_mb / x.mem_total_mb); }
    if (x.disk_total_gb) { $('sysDisk').textContent = `${x.disk_used_gb.toFixed(1)} / ${x.disk_total_gb.toFixed(0)}GB`; meter('sysDiskBar', x.disk_used_gb / x.disk_total_gb); }
    if (x.power_w != null) {  // 막대 끝 = 권장 어댑터. 색은 전압 상태 (W가 작아도 전압이 모자라면 빨강)
      const rec = x.power_w_recommended;
      $('sysPower').textContent = `약 ${x.power_w.toFixed(1)}W`;
      $('sysPowerSub').textContent = `권장 어댑터 ${rec}W (5V ${(rec / 5).toFixed(1)}A)` + (x.undervoltage_now ? ' · 지금 전압 부족' : '');
      meter('sysPowerBar', x.power_w / rec);
      $('sysPowerBar').style.background = x.undervoltage_now ? 'var(--bad)' : x.undervoltage_ever ? 'var(--warn)' : 'var(--ok)';
    }
    const up = x.uptime_sec;
    $('sysSub').textContent = up != null ? `켜진 지 ${up >= 86400 ? Math.floor(up / 86400) + '일 ' : ''}${Math.floor(up % 86400 / 3600)}시간 ${Math.floor(up % 3600 / 60)}분` : '';
    const notes = [];
    if (x.undervoltage_now) notes.push('⚠️ 지금 전원 전압이 부족해요! 5V 2.5A 이상 어댑터와 짧고 굵은 케이블을 쓰세요' + (x.throttled_now ? ' (그래서 CPU 속도도 낮춰진 상태예요)' : ''));
    else if (x.undervoltage_ever) notes.push('⚡ 켜진 뒤로 전압 부족이 있었어요. 어댑터·케이블을 확인해 보세요');
    else if (x.undervoltage_now === false) notes.push('⚡ 전원 정상');
    if (x.temp_limit_now) notes.push('🔥 온도가 높아서 속도를 낮추는 중이에요. 통풍을 확인하세요');
    if (x.db_mb != null) notes.push(`💾 기록 DB ${x.db_mb.toFixed(1)}MB`);
    $('sysFoot').textContent = notes.join(' · ');
  }

  async function refresh() {
    try {
      const r = await fetch('/api/system', { cache: 'no-store' });
      if (r.ok) render(await r.json());  // 로그인이 풀린 건 화면 쪽 새로고침이 처리한다
    } catch (e) {}
  }
  refresh();
  setInterval(refresh, 15000);
})();
