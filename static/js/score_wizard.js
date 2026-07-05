/* 评分分步向导 + 实时进度条 */
(function() {
  const wizardEl = document.getElementById('wizard');
  const TOTAL_STEPS = parseInt(wizardEl?.dataset.totalSteps || '5');
  let currentStep = 1;

  // ===== 步骤切换 =====
  window.goToStep = function(step, skipValidation) {
    if (step < 1 || step > TOTAL_STEPS) return;
    // 验证当前步骤必填项（skipValidation 时跳过）
    if (!skipValidation) {
      const currentPanel = document.querySelector('.step-panel[data-step="' + currentStep + '"]');
      if (step > currentStep) {
        const requireds = currentPanel.querySelectorAll('[required]');
        for (const el of requireds) {
          if (!el.value.trim()) {
            el.style.borderColor = 'var(--danger)';
            el.focus();
            var label = el.closest('.form-group')?.querySelector('label')?.textContent || el.name || '该字段';
            var msg = document.createElement('div');
            msg.className = 'flash error';
            msg.textContent = '⚠️ 请填写：' + label;
            msg.style.cssText = 'position:fixed;top:70px;left:50%;transform:translateX(-50%);z-index:999;animation:fadeOut 3s forwards';
            document.body.appendChild(msg);
            setTimeout(function() { msg.remove(); }, 3000);
            setTimeout(function() { el.style.borderColor = ''; }, 3000);
            return;
          }
        }
      }
    }
    currentStep = step;
    updateUI();
    document.getElementById('wizard').scrollIntoView({behavior: 'smooth', block: 'start'});
  };

  function hasHighTechGrowthData() {
    const form = document.getElementById('score-form');
    if (!form) return false;
    const valueOf = name => (form.querySelector('[name="' + name + '"]')?.value || '').trim();
    const hasThreeYearData = ['year1', 'year2', 'year3'].every(prefix =>
      valueOf(prefix + '_net_assets') && valueOf(prefix + '_sales')
    );
    const hasManualGrowthRates = valueOf('growth_net_assets_rate') && valueOf('growth_sales_rate');
    return hasThreeYearData || hasManualGrowthRates;
  }

  function canSubmitDirectly() {
    const form = document.getElementById('score-form');
    if (!form || !hasHighTechGrowthData()) return false;
    const requireds = form.querySelectorAll('[required]');
    for (const el of requireds) {
      if (el.disabled) continue;
      if (!el.value.trim()) return false;
    }
    return true;
  }

  window.nextStep = function() {
    if (canSubmitDirectly()) {
      const form = document.getElementById('score-form');
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
      return;
    }
    goToStep(currentStep + 1);
  };
  window.prevStep = function() { goToStep(currentStep - 1); };

  // ===== UI 更新 =====
  function updateUI() {
    // 步骤指示器
    document.querySelectorAll('.step-dot').forEach(dot => {
      const s = parseInt(dot.dataset.step);
      dot.classList.remove('active', 'done');
      if (s < currentStep) dot.classList.add('done');
      if (s === currentStep) dot.classList.add('active');
    });
    // 面板
    document.querySelectorAll('.step-panel').forEach(p => {
      p.classList.toggle('active', parseInt(p.dataset.step) === currentStep);
    });
    // 按钮
    const prevBtn = document.getElementById('btn-prev');
    const nextBtn = document.getElementById('btn-next');
    const submitBtn = document.getElementById('btn-submit');
    if (prevBtn) prevBtn.style.display = currentStep === 1 ? 'none' : '';
    if (nextBtn) nextBtn.style.display = currentStep === TOTAL_STEPS ? 'none' : '';
    if (submitBtn) submitBtn.style.display = currentStep === TOTAL_STEPS ? '' : 'none';
  }

  // ===== 实时分数预估 =====
  window.updateLiveScore = updateLiveScore;
  function updateLiveScore() {
    const bar = document.getElementById('live-score-bar');
    if (!bar) return;
    const form = document.getElementById('score-form');
    if (!form) return;

    let total = 0, estimated = 0;
    const scores = {};

    // 遍历所有有分数的表单项
    const selects = form.querySelectorAll('select.grade-select, select[name^="ip_"], select[name^="revenue_"], select[name^="product_"], select[name^="digital_"], select[name^="ip_quality"], select[name^="rd_institution"]');
    selects.forEach(sel => {
      if (!sel.value) return;
      const option = sel.selectedOptions[0];
      if (!option) return;
      const text = option.textContent;
      const m = text.match(/(\d+)[-~]*(\d*)\s*分/);
      if (m) {
        const low = parseInt(m[1]);
        const high = m[2] ? parseInt(m[2]) : low;
        const avg = Math.round((low + high) / 2);
        scores[sel.name] = avg;
      }
    });

    // 遍历数值输入
    const numInputs = form.querySelectorAll('input[type="number"]');
    numInputs.forEach(inp => {
      if (!inp.value || inp.name.endsWith('_score')) return;
      const v = parseFloat(inp.value);
      if (isNaN(v)) return;
      // 简单映射
      const maps = {
        'transform_count': {0:0, 1:3, 2:9, 3:15, 4:21, 5:27},
        'market_years': v => Math.min(Math.floor(v/2), 5),
        'revenue_growth': v => v >= 10 ? 10 : v >= 8 ? 8 : v >= 6 ? 6 : v >= 4 ? 4 : v > 0 ? 2 : 0,
        'net_profit_rate': v => v >= 10 ? 10 : v >= 8 ? 8 : v >= 6 ? 6 : v >= 4 ? 4 : v >= 2 ? 2 : 0,
        'debt_ratio': v => v < 50 ? 5 : v < 60 ? 3 : v < 70 ? 1 : 0,
        'rd_staff_ratio': v => v >= 20 ? 5 : v >= 10 ? 3 : v >= 5 ? 1 : 0,
        'rd_amount': v => v >= 500 ? 10 : v >= 400 ? 8 : v >= 300 ? 6 : v >= 200 ? 4 : v >= 100 ? 2 : 0,
      };
      if (maps[inp.name]) {
        scores[inp.name] = typeof maps[inp.name] === 'function' ? maps[inp.name](v) : (maps[inp.name][v] || maps[inp.name][Math.floor(v)] || 0);
      }
    });

    // 手动分数覆盖
    form.querySelectorAll('input[name$="_score"]').forEach(inp => {
      if (inp.value) {
        const baseName = inp.name.replace('_score', '');
        scores[baseName + '_manual'] = parseInt(inp.value);
      }
    });

    total = Object.values(scores).reduce((a, b) => a + b, 0);
    // 粗略估算：总分按选中字段的最大分求和
    estimated = Math.min(total, 100);

    document.getElementById('live-total').textContent = estimated || '—';
    const passLine = parseInt(bar.dataset.passLine || '71');
    const fill = document.getElementById('live-fill');
    fill.style.width = estimated + '%';
    fill.className = 'progress-fill ' + (estimated >= passLine ? 'high' : estimated >= passLine * 0.7 ? 'mid' : 'low');
  }

  // 监听表单变化
  const form = document.getElementById('score-form');
  if (form) {
    form.addEventListener('input', updateLiveScore);
    form.addEventListener('change', updateLiveScore);
  }

  // ===== 等级选择 → 自动填入分数 =====
  form.addEventListener('change', function(e) {
    const sel = e.target;
    if (sel.tagName !== 'SELECT' || sel.name.endsWith('_score')) return;
    const scoreInput = form.querySelector('[name="' + sel.name + '_score"]');
    if (!scoreInput) return;
    // IP 自动评估已填分数，不再覆盖
    if (scoreInput.dataset.ipAuto === '1') return;
    const option = sel.selectedOptions[0];
    if (!option) return;
    const m = option.textContent.match(/(\d+)[-~]*(\d*)\s*分/);
    if (m) {
      const low = parseInt(m[1]);
      const high = m[2] ? parseInt(m[2]) : low;
      scoreInput.value = Math.round((low + high) / 2);
      scoreInput.dispatchEvent(new Event('input', {bubbles: true}));
    }
  });

  // ===== 键盘导航 =====
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA' && e.target.tagName !== 'BUTTON') {
      e.preventDefault();
      if (currentStep < TOTAL_STEPS) nextStep();
    }
  });

  // 初始化
  updateUI();
  updateLiveScore();
  

})();
