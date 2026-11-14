/* 工作区设置 → 定时任务页签 */
(function () {
  'use strict';

  const state = {
    workspaceUuid: '',
    jobs: [],
    editingKey: '',
    bound: false,
  };

  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function setStatus(text, type) {
    const el = $('cron-status');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'settings-status' + (type ? ' ' + type : '');
  }

  async function api(path, options) {
    const response = await fetch(path, options);
    let data = {};
    try {
      data = await response.json();
    } catch (e) {
      data = {};
    }
    if (!response.ok) {
      throw new Error(data.detail || ('HTTP ' + response.status));
    }
    return data;
  }

  function jobKey(item) {
    return (item.workspace_uuid || state.workspaceUuid) + '|' + item.id;
  }

  function splitKey(key) {
    const index = key.indexOf('|');
    return { workspaceUuid: key.slice(0, index), jobId: key.slice(index + 1) };
  }

  async function load() {
    if (!state.workspaceUuid) return;
    const loadingText = '加载中...';
    setStatus(loadingText, 'info');
    try {
      const data = await api('/api/workspaces/' + encodeURIComponent(state.workspaceUuid) + '/cron/jobs');
      state.jobs = data.jobs || [];
      render();
      if ($('cron-status').textContent === loadingText) setStatus('');
    } catch (error) {
      setStatus('✗ 加载失败: ' + error.message, 'error');
    }
  }

  function render() {
    const list = $('cron-task-list');
    if (!list) return;
    if (!state.jobs.length) {
      list.innerHTML = '<div class="cron-empty">暂无定时任务</div>';
      return;
    }
    list.innerHTML = state.jobs.map(renderCard).join('');
  }

  function renderCard(item) {
    const s = item.state || {};
    const key = esc(jobKey(item));
    const managed = !!item.managed;
    const badges = [];
    if (managed) {
      badges.push('<span class="cron-badge readonly">全局任务 · 只读</span>');
    } else {
      badges.push(
        '<span class="cron-badge ' + (item.enabled ? 'on' : 'off') + '">' +
        (item.enabled ? '启用' : '停用') + '</span>'
      );
    }
    if (item.one_time) badges.push('<span class="cron-badge">一次性</span>');

    let taskText = '';
    if (item.content && typeof item.content === 'object' && item.content.task) {
      taskText = String(item.content.task);
    }
    if (taskText.length > 160) taskText = taskText.slice(0, 160) + '...';

    const actions = [];
    if (!managed) {
      actions.push('<label class="cron-toggle"><input type="checkbox" data-action="toggle" ' +
        (item.enabled ? 'checked' : '') + '> 启用</label>');
      actions.push('<button type="button" class="btn btn-small btn-secondary" data-action="run">立即运行</button>');
    }
    actions.push('<button type="button" class="btn btn-small btn-secondary" data-action="history">历史</button>');
    if (!managed) {
      actions.push('<button type="button" class="btn btn-small" data-action="edit">编辑</button>');
      actions.push('<button type="button" class="btn btn-small btn-danger" data-action="delete">删除</button>');
    }

    return '' +
      '<div class="cron-card" data-key="' + key + '">' +
        '<div class="cron-card-head">' +
          '<span class="cron-name">' + esc(item.name || item.id) + '</span>' +
          badges.join('') +
        '</div>' +
        (item.description ? '<div class="cron-desc">' + esc(item.description) + '</div>' : '') +
        '<div class="cron-meta">' + esc(item.schedule_summary || '') + '</div>' +
        '<div class="cron-meta">下次：' + esc(s.next_run || '-') +
          ' · 上次：' + esc(s.last_status || '-') +
          (s.last_run ? ' (' + esc(s.last_run) + ')' : '') +
          ' · 投递：' + esc((item.delivery || {}).mode || 'none') +
        '</div>' +
        (taskText ? '<div class="cron-task-text">' + esc(taskText) + '</div>' : '') +
        '<div class="cron-actions">' + actions.join('') + '</div>' +
        '<div class="cron-history" style="display:none;"></div>' +
      '</div>';
  }

  function cardOf(element) {
    return element.closest ? element.closest('.cron-card') : null;
  }

  async function onAction(event) {
    const actionEl = event.target.closest('[data-action]');
    if (!actionEl) return;
    const card = cardOf(actionEl);
    if (!card) return;
    const action = actionEl.getAttribute('data-action');
    const ref = splitKey(card.getAttribute('data-key'));
    const base = '/api/workspaces/' + encodeURIComponent(ref.workspaceUuid) +
      '/cron/jobs/' + encodeURIComponent(ref.jobId);

    try {
      if (action === 'toggle') {
        const enable = actionEl.checked;
        await api(base + (enable ? '/enable' : '/disable'), { method: 'POST' });
        setStatus(enable ? '已启用' : '已停用', 'success');
        await load();
      } else if (action === 'run') {
        await api(base + '/run', { method: 'POST' });
        setStatus('已触发，稍后刷新查看结果', 'info');
      } else if (action === 'delete') {
        if (!confirm('确定删除该定时任务？历史运行记录会一并删除。')) return;
        await api(base, { method: 'DELETE' });
        setStatus('已删除', 'success');
        await load();
      } else if (action === 'edit') {
        const item = state.jobs.find((job) => jobKey(job) === card.getAttribute('data-key'));
        if (item) openForm(item);
      } else if (action === 'history') {
        await loadHistory(base, card);
      }
    } catch (error) {
      setStatus('✗ ' + error.message, 'error');
    }
  }

  async function loadHistory(base, card) {
    const panel = card.querySelector('.cron-history');
    if (!panel) return;
    if (panel.style.display !== 'none' && panel.dataset.loaded === '1') {
      panel.style.display = 'none';
      return;
    }
    try {
      const data = await api(base + '/history?limit=20');
      const records = data.history || [];
      if (!records.length) {
        panel.innerHTML = '<div class="cron-empty">暂无运行记录</div>';
      } else {
        panel.innerHTML = records.map(function (r) {
          return '<div class="cron-history-row">' +
            '<span>' + esc(r.slot || '-') + '</span>' +
            '<span>' + esc(r.status || '-') + '</span>' +
            '<span>' + esc(r.duration_ms == null ? '-' : r.duration_ms + 'ms') + '</span>' +
            '<span>' + esc(r.delivery || '-') + '</span>' +
            (r.error ? '<span class="cron-history-error">' + esc(r.error) + '</span>' : '') +
            '</div>';
        }).join('');
      }
      panel.dataset.loaded = '1';
      panel.style.display = '';
    } catch (error) {
      setStatus('✗ ' + error.message, 'error');
    }
  }

  function currentType() {
    return $('cron-schedule-type').value;
  }

  function syncScheduleFields() {
    const type = currentType();
    $('cron-fields-interval').style.display = type === 'interval' ? '' : 'none';
    $('cron-fields-cron').style.display = type === 'cron' ? '' : 'none';
    $('cron-fields-at').style.display = type === 'at' ? '' : 'none';
  }

  function syncDeliveryFields() {
    const mode = $('cron-delivery-mode').value;
    $('cron-delivery-session-fields').style.display = mode === 'session' ? '' : 'none';
  }

  function openForm(item) {
    state.editingKey = item ? jobKey(item) : '';
    $('cron-form-title').textContent = item ? '编辑定时任务' : '新建定时任务';
    $('cron-form-key').value = state.editingKey;
    $('cron-name').value = item ? (item.name || '') : '';
    $('cron-name').disabled = !!item;
    $('cron-description').value = item ? (item.description || '') : '';

    const schedule = (item && item.schedule) || { type: 'interval', minutes: 60 };
    $('cron-schedule-type').value = schedule.type || 'interval';
    $('cron-interval-minutes').value = schedule.minutes || 60;
    $('cron-initial-delay').value = schedule.initial_delay_minutes || '';
    $('cron-expr').value = schedule.expr || '';
    $('cron-timezone').value = schedule.timezone || '';
    $('cron-at-time').value = schedule.time || '';

    const content = (item && item.content) || {};
    $('cron-task').value = content.task || '';
    $('cron-plan').value = (content.plan || []).join('\n');

    const delivery = (item && item.delivery) || { mode: 'none' };
    $('cron-delivery-mode').value = delivery.mode || 'none';
    $('cron-delivery-ws').value = delivery.workspace_uuid || '';
    $('cron-delivery-session').value = delivery.session_id || '';

    $('cron-one-time').checked = item ? !!item.one_time : true;
    $('cron-max-executions').value = (item && item.execution && item.execution.max_executions) || 9999;
    $('cron-timeout').value = (item && item.execution && item.execution.timeout_seconds != null)
      ? item.execution.timeout_seconds : 900;

    syncScheduleFields();
    syncDeliveryFields();
    $('cron-task-list').style.display = 'none';
    $('cron-form').style.display = 'block';
    $('cron-form').scrollTop = 0;
  }

  function closeForm() {
    state.editingKey = '';
    $('cron-form').style.display = 'none';
    $('cron-task-list').style.display = '';
  }

  function buildSchedule() {
    const type = currentType();
    if (type === 'interval') {
      const schedule = {
        type: 'interval',
        minutes: parseInt($('cron-interval-minutes').value, 10) || 60,
      };
      const delay = parseInt($('cron-initial-delay').value, 10);
      if (delay > 0) schedule.initial_delay_minutes = delay;
      return schedule;
    }
    if (type === 'cron') {
      const schedule = { type: 'cron', expr: $('cron-expr').value.trim() };
      const tz = $('cron-timezone').value.trim();
      if (tz) schedule.timezone = tz;
      return schedule;
    }
    const schedule = { type: 'at', time: $('cron-at-time').value.trim() };
    const tz = $('cron-timezone').value.trim();
    if (tz) schedule.timezone = tz;
    return schedule;
  }

  async function save() {
    const name = $('cron-name').value.trim();
    if (!name) {
      setStatus('名称不能为空', 'error');
      return;
    }
    const task = $('cron-task').value.trim();
    if (!task) {
      setStatus('任务描述不能为空', 'error');
      return;
    }
    const mode = $('cron-delivery-mode').value;
    const delivery = { mode: mode };
    if (mode === 'session') {
      delivery.workspace_uuid = $('cron-delivery-ws').value.trim() || state.workspaceUuid;
      delivery.session_id = $('cron-delivery-session').value.trim();
      if (!delivery.session_id) {
        setStatus('指定会话时必须填写会话 ID', 'error');
        return;
      }
    }

    const payload = {
      name: name,
      description: $('cron-description').value.trim(),
      schedule: buildSchedule(),
      content: {
        task: task,
        plan: $('cron-plan').value.split('\n').map((line) => line.trim()).filter(Boolean),
      },
      delivery: delivery,
      one_time: $('cron-one-time').checked,
      execution: {
        max_executions: parseInt($('cron-max-executions').value, 10) || 9999,
        timeout_seconds: parseInt($('cron-timeout').value, 10),
      },
    };
    if (isNaN(payload.execution.timeout_seconds) || payload.execution.timeout_seconds < 0) {
      payload.execution.timeout_seconds = 900;
    }

    try {
      if (state.editingKey) {
        const ref = splitKey(state.editingKey);
        await api(
          '/api/workspaces/' + encodeURIComponent(ref.workspaceUuid) +
            '/cron/jobs/' + encodeURIComponent(ref.jobId),
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              description: payload.description,
              schedule: payload.schedule,
              content: payload.content,
              delivery: payload.delivery,
              one_time: payload.one_time,
              execution: payload.execution,
            }),
          }
        );
      } else {
        await api(
          '/api/workspaces/' + encodeURIComponent(state.workspaceUuid) + '/cron/jobs',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          }
        );
      }
      setStatus('✓ 已保存', 'success');
      closeForm();
      await load();
    } catch (error) {
      setStatus('✗ ' + error.message, 'error');
    }
  }

  function bind() {
    if (state.bound) return;
    state.bound = true;
    $('cron-new-btn').addEventListener('click', function () {
      openForm(null);
    });
    $('cron-refresh-btn').addEventListener('click', load);
    $('cron-task-list').addEventListener('click', onAction);
    $('cron-task-list').addEventListener('change', function (event) {
      if (event.target && event.target.getAttribute('data-action') === 'toggle') {
        onAction(event);
      }
    });
    $('cron-schedule-type').addEventListener('change', syncScheduleFields);
    $('cron-delivery-mode').addEventListener('change', syncDeliveryFields);
    $('cron-save-btn').addEventListener('click', save);
    $('cron-cancel-btn').addEventListener('click', closeForm);
  }

  window.CronSettings = {
    init: function (workspaceUuid) {
      bind();
      $('cron-form').style.display = 'none';
      $('cron-task-list').style.display = '';
      state.workspaceUuid = workspaceUuid || '';
      load();
    },
    reload: load,
  };
})();
