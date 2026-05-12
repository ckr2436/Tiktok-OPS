import HermesAgentPage from './HermesAgentPage.jsx'

export default function GeoPage() {
  return (
    <HermesAgentPage
      title="GEO / AI 搜索优化助手"
      description="围绕实体与语义关系生成 GEO 策略，提升 AI 搜索引擎中的可检索性与引用率。"
      endpoint="geo"
      permissionKey="hermes_agent.geo"
      fields={[
        { name: 'brand', label: '品牌名称', required: true },
        { name: 'entity', label: '核心实体', required: true, placeholder: '例如：功效成分 / 使用场景 / 用户痛点' },
        { name: 'topic', label: '优化主题', required: true, placeholder: '例如：敏感肌修护' },
        { name: 'seed_content', label: '已有内容（可选）', rows: 5, placeholder: '粘贴已有介绍或文案，便于增强分析' },
      ]}
    />
  )
}
