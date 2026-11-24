const assert=require('node:assert/strict');
const registry=require('../src/module-registry.json');
const catalog=require('../src/catalog.json');
const ids=new Set();
for(const m of registry){
 assert(!ids.has(m.id),'duplicate module ID: '+m.id);ids.add(m.id);
 assert(m.integration==='navigation-only','real integration requires separate acceptance');
 const group=catalog.find(([category])=>category===m.category);
 assert(group&&group[1].some(([name,,screen])=>name===m.name&&screen===m.screen),'catalog mismatch: '+m.id);
}
const existing={t00:'数据连接',t01:'数据管道',t02:'数据质量与血缘',t10:'本体生成与管理',t11:'术语与指标',t12:'规则 函数与动作',t20:'对象探索',t21:'分析与时间序列',t22:'情景推演',t30:'智能体开发',t31:'AI逻辑与评估',t32:'模型与工具管理',t40:'应用搭建工作台',t41:'自动化',t42:'审批',t50:'模板与实施包',t60:'平台管理',t61:'运行与审计'};
for(const [id,name]of Object.entries(existing))assert.equal(registry.find(m=>m.id===id)?.name,name,'existing deep link changed: '+id);
assert.equal(registry.length,catalog.reduce((n,[,items])=>n+items.length,0));
console.log('PASS: explicit stable IDs, 18 existing links, registry/catalog coverage and integration boundary');
