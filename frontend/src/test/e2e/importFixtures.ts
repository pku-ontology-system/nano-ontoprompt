// What the import API answers in the e2e specs: the catalogue of prepared data sets, and the job of importing one.
export const BUNDLES = [
  { id: 'demo-tpch', title: '供应链订单履约（TPC-H）', summary: '客户、订单、零件和供应商的多表业务库。', domain: '供应链', kind: 'relational', tags: ['多表', '真实基准'],
    shows: ['12 个对象类型', '很长的血缘链条'], replay: false, sources: [{ id: 'tpch', title: 'TPC-H 数据生成器', kind: 'generated' }], imported: null },
  { id: 'demo-nyc-taxi', title: '出租车行程事件流（纽约 TLC）', summary: '一月的黄色出租车行程，按时间逐步载入。', domain: '出行', kind: 'events', tags: ['事件流'],
    shows: ['按时间逐步载入'], replay: true, sources: [{ id: 'tlc', title: 'NYC TLC 行程样本', kind: 'file' }], imported: { ontology_id: 'ont-taxi', name: '出租车行程事件流（纽约 TLC）', objects: 946 } },
  { id: 'demo-brick-site-b', title: '楼宇设备与传感时序（Brick）', summary: '楼宇的设备、空间、点位和传感时序。', domain: '楼宇', kind: 'timeseries', tags: ['时序'], shows: ['时序观测'], replay: false,
    sources: [{ id: 'brick', title: 'Brick 数据集', kind: 'file' }], imported: null },
  { id: 'demo-abt-buy', title: '商品主数据合并（Abt-Buy）', summary: '两家网店的商品目录，有已知同款的标准答案。', domain: '供应链', kind: 'multisource', tags: ['实体消歧', '有标准答案'],
    shows: ['同款商品'], replay: false, sources: [{ id: 'abt-buy', title: 'Abt-Buy.zip', kind: 'file' }], imported: null },
  { id: 'demo-beans', title: '豆叶病害照片（图像 + 标签）', summary: '60 张叶片照片和病害标签。', domain: '农业', kind: 'multimodal', tags: ['图像'], shows: ['图片资产'], replay: false,
    sources: [{ id: 'beans', title: 'beans 数据集', kind: 'media' }], imported: null },
]

const STEPS = ['读取数据', '识别类型、主键和关系', '创建本体', '发布对象类型和关系', '写入对象', '建立关系', '核对结果', '生成数据血缘']
export const jobAt = (done: number, status: 'running' | 'done' | 'failed' = 'running') => ({
  id: 'job-1', title: '供应链订单履约（TPC-H）', status, started_at: 0, finished_at: status === 'done' ? 3 : null, error: status === 'failed' ? '图数据库不可用' : null,
  steps: STEPS.map((title, i) => ({ key: `s${i}`, title, status: i < done ? 'done' : i === done && status === 'running' ? 'running' : 'pending', detail: i < done ? `${title}：完成` : null, seconds: i < done ? 0.2 : null })),
  log: ['12:00:00 读取完成'],
  result: status === 'done' ? { ontology_id: 'ont-new', ontology_name: '供应链订单履约（TPC-H）', objects: 7498, types: 12, links: 13, link_instances: 16089, counts: { Customer: 150 }, replay: false, bundle: 'demo-tpch' } : null,
})
