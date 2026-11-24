/* 工厂场景演示数据。传感器通道参考 NASA C-MAPSS 公开数据集，其余为虚构。 */
window.D = (function () {
  // ---------- 实体类型目录 ----------
  // attrs: [名称, 数据类型, 来源字段, 样本, 识别方式]
  const TYPES = {
    line: { name: '产线', color: '#7B61FF', n: 4, attrs: [['产线编号', '文本', 'line_id', 'L2', '声明主键'], ['名称', '文本', 'name', '二号装配线', '画像规则'], ['所属车间', '文本', 'workshop', '一车间', '画像规则']] },
    eq: { name: '设备', color: '#1F8FB3', n: 36, attrs: [['设备编号', '文本', 'equip_id', 'EQ003', '声明主键'], ['设备名称', '文本', 'name', '3 号压缩机', '画像规则'], ['型号', '文本', 'model', 'CFM56-7B', '画像规则'], ['投产日期', '日期', 'install_date', '2019-06-01', '画像规则'], ['状态', '枚举（带有效期）', 'status / dwd_equip_status', '正常 / 关注 / 高风险 / 停机', '码表 + 时序识别']] },
    point: { name: '测点', color: '#22A88E', n: 144, attrs: [['测点编号', '文本', '列名生成', 'EQ003.T50', '时序识别'], ['测量量', '文本', 't50 列注释', '低压涡轮出口温度', '大模型补全'], ['单位', '文本', '列注释', '°R', '画像规则'], ['采样间隔', '时长', 'ts 差分', '10 秒', '画像规则'], ['窗口统计', '数值', '按 10 分钟窗口计算', '均值 · 峰值 · 越线次数', '时序识别']] },
    wo: { name: '工单', color: '#E89A2E', n: 4200, attrs: [['工单号', '文本', 'wo_id', 'WO-88012', '声明主键'], ['类型', '枚举', 'type', '检修 / 保养 / 点检', '码表'], ['状态', '枚举', 'state', '待派 / 处理中 / 完成', '码表'], ['创建时间', '日期时间', 'created_at', '2026-11-02 08:10', '画像规则']] },
    mt: { name: '维保记录', color: '#E2685A', n: 3412, attrs: [['维保编号', '文本', 'mt_id', 'MT-20261102-07', '声明主键'], ['开始时间', '日期时间', 'start_ts', '2026-11-02 08:30', '画像规则'], ['维保类型', '枚举', 'kind', '更换 / 润滑 / 校准', '码表'], ['更换部件', '文本', 'part', '2 号轴承', '大模型补全']] },
    staff: { name: '人员', color: '#8796AA', n: 120, attrs: [['工号', '文本', 'staff_id', 'U-1127', '声明主键'], ['姓名', '文本', 'name', '（已脱敏）', '画像规则'], ['班组', '文本', 'team', '机修二班', '画像规则']] },
    event: { name: '高温事件', color: '#D9443A', n: 87, attrs: [['事件编号', '文本', '规则生成', 'EV-0412', '时序识别'], ['开始时间', '日期时间', '窗口起点', '2026-11-02 06:00', '时序识别'], ['结束时间', '日期时间', '窗口终点', '2026-11-02 07:30', '时序识别'], ['峰值', '数值', 't50 窗口峰值', '1,612 °R', '时序识别'], ['触发规则', '文本', '健康段统计', 'T50 连续 3 个窗口 > 1,598 °R', '时序识别']] },
    ins: { name: '巡检', color: '#A777E3', n: 1890, attrs: [['巡检编号', '文本', 'ins_id', 'IN-1101-3', '声明主键'], ['设备编码', '文本', 'equip_code', 'EQ-003', '画像规则'], ['结论', '枚举', 'result', '正常 / 关注 / 异常', '码表']] },
    meter: { name: '电表', color: '#C9A227', n: 48, attrs: [['电表编号', '文本', 'meter_id', 'M-012', '声明主键'], ['日用电量', '时间序列', 'dwd_energy_daily.kwh', '812.4 kWh', '时序识别']] },
    tariff: { name: '电价', color: '#9AA5B1', n: 6, attrs: [['电价编号', '文本', 'tariff_id', 'T1', '声明主键'], ['单价', '数值', 'price', '0.82 元/kWh', '画像规则']] },
  };
  // 每种类型的英文表名（大模型补全前显示）
  const TABLE_LABEL = { line: 'dwd_line', eq: 'dwd_equipment', point: 'sensor.*', wo: 'dwd_work_order', mt: 'dwd_maintenance', staff: 'dwd_staff', event: 't50 > 阈值', ins: 'mes.inspection', meter: 'dwd_meter', tariff: 'dim_tariff' };
  // 关系目录：[起点, 终点, 名称, 识别方式, 实例数, 依据]
  const RELS = [
    ['eq', 'line', '属于', '声明外键', 36, 'dwd_equipment.line_id → dwd_line.line_id，外键完整率 100%'],
    ['point', 'eq', '安装于', '时序识别', 144, '传感器宽表按 equip_id 分组，每个数值列是该设备的一个测点'],
    ['wo', 'eq', '针对', '声明外键', 4200, 'dwd_work_order.equip_id → dwd_equipment.equip_id，外键完整率 100%'],
    ['mt', 'eq', '针对', '声明外键', 3412, 'dwd_maintenance.equip_id → dwd_equipment.equip_id，外键完整率 100%'],
    ['mt', 'staff', '负责人', '推断', 3341, '没有外键；owner 与 staff_id 名称相近，值域重合 98%'],
    ['wo', 'staff', '指派给', '推断', 4116, '没有外键；assignee 与 staff_id 值域重合 98%'],
    ['event', 'eq', '发生于', '时序识别', 87, '由 T50 窗口统计按规则生成，事件带有效时间'],
    ['ins', 'eq', '检查', '推断', 1833, '没有外键；equip_code 去掉连字符后与 equip_id 值域重合 97%'],
    ['meter', 'eq', '计量', '声明外键', 48, 'dwd_meter.equip_id → dwd_equipment.equip_id'],
  ];

  // ---------- 数据源 ----------
  // 表：{ name, cn, rows, kind: 'basic'|'ts'|'state', to: [类型], cols: [[字段, 类型, 说明, 样本, 标记]] }
  const T = (name, cn, rows, kind, to, cols) => ({ name, cn, rows, kind, to, cols });
  const sources = [
    { id: 'dp', name: '工厂主题库', note: '数据中台 · 设备主题资产', kind: '数据中台', access: '中台物化完成后回调', updated: '11-17 02:00', live: true,
      meta: { owner: '设备部数据组', job: 'eq_theme_daily（每日 02:00）', lineage: 'MES / SCADA → ods → dwd → 主题库', quality: '94 / 100' },
      tables: [
        T('dwd_equipment', '设备台账', 36, 'basic', ['eq'], [['equip_id', 'varchar', '设备编号', 'EQ003', 'pk'], ['name', 'varchar', '设备名称', '3 号压缩机'], ['model', 'varchar', '型号', 'CFM56-7B'], ['line_id', 'varchar', '所属产线', 'L2', 'fk:dwd_line'], ['install_date', 'date', '投产日期', '2019-06-01'], ['status', 'varchar', '状态（码表 EQ_STATUS）', '正常', 'code']]),
        T('dwd_line', '产线', 4, 'basic', ['line'], [['line_id', 'varchar', '产线编号', 'L2', 'pk'], ['name', 'varchar', '产线名称', '二号装配线'], ['workshop', 'varchar', '所属车间', '一车间']]),
        T('dwd_work_order', '工单', 4200, 'basic', ['wo'], [['wo_id', 'varchar', '工单号', 'WO-88012', 'pk'], ['equip_id', 'varchar', '设备编号', 'EQ003', 'fk:dwd_equipment'], ['type', 'varchar', '工单类型（码表 WO_TYPE）', '检修', 'code'], ['state', 'varchar', '状态', '处理中', 'code'], ['created_at', 'timestamp', '创建时间', '2026-11-02 08:10'], ['assignee', 'varchar', '处理人', 'U-1127']]),
        T('dwd_maintenance', '维保记录', 3412, 'basic', ['mt'], [['mt_id', 'varchar', '维保编号', 'MT-20261102-07', 'pk'], ['equip_id', 'varchar', '设备编号', 'EQ003', 'fk:dwd_equipment'], ['start_ts', 'timestamp', '开始时间', '2026-11-02 08:30'], ['kind', 'varchar', '维保类型', '更换', 'code'], ['part', 'varchar', '更换部件', '2 号轴承'], ['owner', 'varchar', '负责人', 'U-1127']]),
        T('dwd_staff', '人员', 120, 'basic', ['staff'], [['staff_id', 'varchar', '工号', 'U-1127', 'pk'], ['name', 'varchar', '姓名（已脱敏）', '张*'], ['team', 'varchar', '班组', '机修二班']]),
        T('ods_sensor_reading', '传感器读数', 19200000, 'ts', ['point', 'event'], [['ts', 'timestamp', '采样时间', '2026-11-02 06:00:10'], ['equip_id', 'varchar', '设备编号', 'EQ003'], ['t24', 'float', 'LPC 出口温度 °R', '642.3'], ['t30', 'float', 'HPC 出口温度 °R', '1589.7'], ['t50', 'float', 'LPT 出口温度 °R', '1401.2'], ['nc', 'float', '核心转速 rpm', '9046.2']]),
        T('dwd_equip_status', '设备状态变更', 9120, 'state', ['eq'], [['equip_id', 'varchar', '设备编号', 'EQ003'], ['status', 'varchar', '状态', '高风险', 'code'], ['effective_at', 'timestamp', '生效时间', '2026-11-02 06:00']]),
      ] },
    { id: 'mes', name: 'MES 业务库', note: 'PostgreSQL 15 · 只读账号', kind: '业务数据库', access: '变更捕获（逻辑复制）', updated: '11-17 02:05', live: true,
      meta: { owner: '生产信息科', job: '实时（复制槽 onto_mes）', lineage: '车间 MES 主库', quality: '—' },
      tables: [
        T('mes.equipment', '设备', 36, 'basic', ['eq'], [['id', 'varchar', '设备编号', 'EQ003', 'pk'], ['name', 'varchar', '名称', '3 号压缩机'], ['line', 'varchar', '产线', 'L2']]),
        T('mes.work_order', '工单', 4200, 'basic', ['wo'], [['id', 'varchar', '工单号', 'WO-88012', 'pk'], ['eq_id', 'varchar', '设备', 'EQ003', 'fk:mes.equipment'], ['state', 'varchar', '状态', '处理中']]),
        T('mes.inspection', '巡检', 1890, 'basic', ['ins'], [['ins_id', 'varchar', '巡检编号', 'IN-1101-3', 'pk'], ['equip_code', 'varchar', '设备编码（无外键）', 'EQ-003'], ['result', 'varchar', '结论', '关注']]),
      ] },
    { id: 'mq', name: '车间传感器实时流', note: 'MQTT · plant/+/metrics', kind: '时序数据', access: '订阅，按 10 秒聚合', updated: '11-17 02:05', live: true,
      meta: { owner: '设备部', job: '持续消费', lineage: 'PLC → 边缘网关 → MQTT', quality: '—' },
      tables: [T('plant.metrics', '测点读数（长表）', 31200000, 'ts', ['point', 'event'], [['ts', 'timestamp', '时间', '02:05:30'], ['point', 'varchar', '测点', 'EQ003.T50'], ['value', 'float', '值', '1401.2'], ['unit', 'varchar', '单位', '°R']])] },
    { id: 'xls', name: '设备清单.xlsx', note: '文件 · 11-10 上传', kind: '文件', access: '上传', updated: '11-10 09:12', live: false,
      meta: { owner: '设备部', job: '一次性', lineage: '手工整理', quality: '—' },
      tables: [T('设备清单', '设备清单（Sheet1）', 36, 'basic', ['eq'], [['设备编号', 'text', '', 'EQ003', 'pk'], ['名称', 'text', '', '3 号压缩机'], ['供应商', 'text', '', '华东动力']])] },
  ];

  // 中台资产目录（连接数据中台时看到）
  const catalog = [
    { name: '工厂主题库', path: 'eq_theme/*', tables: 7, reg: 'dp' },
    { name: '能耗主题库', path: 'energy_theme/*', tables: 3, tablesDef: [
      T('dwd_meter', '电表', 48, 'basic', ['meter'], [['meter_id', 'varchar', '电表编号', 'M-012', 'pk'], ['equip_id', 'varchar', '设备编号', 'EQ003', 'fk:dwd_equipment']]),
      T('dwd_energy_daily', '日能耗', 52560, 'ts', ['meter'], [['day', 'date', '日期', '2026-11-16'], ['meter_id', 'varchar', '电表', 'M-012'], ['kwh', 'float', '用电量', '812.4']]),
      T('dim_tariff', '电价', 6, 'basic', ['tariff'], [['tariff_id', 'varchar', '电价编号', 'T1', 'pk'], ['price', 'float', '单价', '0.82']])] },
    { name: '质量主题库', path: 'quality_theme/*', tables: 4 },
  ];

  // ---------- 预置本体（已从工厂主题库生成，持续更新中） ----------
  const initialOnto = () => ({ id: 'o1', name: '工厂设备本体', src: ['dp'], types: ['line', 'eq', 'point', 'wo', 'mt', 'staff', 'event'], modes: ['basic', 'ts'], dynamic: true,
    created: '10-20 09:30', updated: '02:05', extra: {}, pendingChange: true });

  // 时间视图：每天的变化量（10-01 起），以及 EQ003 的事实记录
  let sd = 7; const rnd = () => (sd = (sd * 16807) % 2147483647) / 2147483647;
  const daily = Array.from({ length: 48 }, (_, d) => d < 19 ? 0 : Math.round(380 + rnd() * 420 + (d % 7 >= 5 ? -260 : 0) + (d === 19 ? 6200 : 0)));
  const facts = [
    { n: 0, st: '正常', v0: '10-20', v1: null, r0: '10-20', r1: null, note: '首次装载' },
    { n: 1, st: '正常', v0: '11-02', v1: null, r0: '11-03', r1: '11-05', note: '维保记录：11-02 更换 2 号轴承后正常' },
    { n: 2, st: '高风险', v0: '11-02', v1: null, r0: '11-05', r1: null, note: '迟到的 T50 数据：11-02 起连续 3 个窗口超阈值' },
  ];
  // EQ003 T50 窗口均值（每 6 小时一个点，10-20 起），带退化趋势
  const t50 = Array.from({ length: 112 }, (_, i) => +(1560 + i * 0.2 + Math.sin(i / 3) * 5 + (rnd() - .5) * 7 + (i > 52 && i < 60 ? 38 : 0)).toFixed(1));

  // ---------- 实例（实例星图用，按种子生成，约 570 个） ----------
  function instances(types) {
    let s = 11; const r = () => (s = (s * 48271) % 2147483647) / 2147483647;
    const has = t => types.includes(t); const nodes = []; const edges = [];
    const day = () => { const d = 20 + Math.floor(r() * 29); return d <= 31 ? '10-' + d : '11-' + String(d - 31).padStart(2, '0'); };
    const staff = []; if (has('staff')) for (let i = 0; i < 40; i++) { const id = 'U-' + (1101 + i); staff.push(id); nodes.push({ id, t: 'staff', label: id, line: -1, d: '10-20' }); }
    const pick = () => staff[Math.floor(r() * staff.length)];
    for (let l = 1; l <= 4; l++) {
      const lid = 'L' + l; if (has('line')) nodes.push({ id: lid, t: 'line', label: ['一号机加线', '二号装配线', '三号涂装线', '四号测试线'][l - 1], line: l, d: '10-20' });
      for (let k = 0; k < 9; k++) {
        const no = (l - 1) * 9 + k + 1; const eid = 'EQ' + String(no).padStart(3, '0'); if (!has('eq')) continue;
        nodes.push({ id: eid, t: 'eq', label: eid, line: l, d: '10-20', risk: no === 3 || no === 17 || no === 28 });
        if (has('line')) edges.push([eid, lid, '属于']);
        if (has('point')) for (const ch of ['T24', 'T30', 'T50', 'Nc']) { const pid = eid + '.' + ch; nodes.push({ id: pid, t: 'point', label: ch, line: l, d: '10-20', par: eid }); edges.push([pid, eid, '安装于']); }
        const nwo = 2 + Math.floor(r() * 3); if (has('wo')) for (let i = 0; i < nwo; i++) { const id = 'WO-' + (88000 + no * 10 + i); nodes.push({ id, t: 'wo', label: id, line: l, d: day(), par: eid }); edges.push([id, eid, '针对']); if (staff.length) edges.push([id, pick(), '指派给']); }
        const nmt = 1 + Math.floor(r() * 3); if (has('mt')) for (let i = 0; i < nmt; i++) { const id = 'MT-' + no + '-' + i; nodes.push({ id, t: 'mt', label: id, line: l, d: day(), par: eid }); edges.push([id, eid, '针对']); if (staff.length) edges.push([id, pick(), '负责人']); }
        const nev = no === 3 ? 4 : (r() < .55 ? 1 + Math.floor(r() * 3) : 0); if (has('event')) for (let i = 0; i < nev; i++) { const id = 'EV-' + no + '-' + i; nodes.push({ id, t: 'event', label: id, line: l, d: no === 3 && i === 0 ? '11-02' : day(), par: eid }); edges.push([id, eid, '发生于']); }
        if (has('ins') && r() < .8) { const id = 'IN-' + no; nodes.push({ id, t: 'ins', label: id, line: l, d: day(), par: eid }); edges.push([id, eid, '检查']); }
        if (has('meter')) { const id = 'M-' + String(no).padStart(3, '0'); nodes.push({ id, t: 'meter', label: id, line: l, d: '10-20', par: eid }); edges.push([id, eid, '计量']); }
      }
    }
    return { nodes, edges };
  }

  return { TYPES, RELS, TABLE_LABEL, sources, catalog, initialOnto, daily, facts, t50, instances, THRESH: 1598 };
})();
