import HermesAgentPage from './HermesAgentPage.jsx'

export default function SeoPage() {
  return (
    <HermesAgentPage
      title="品牌 SEO 助手"
      description="输入品牌与产品信息，生成站内外 SEO 关键词规划、内容选题与落地建议。"
      endpoint="seo"
      permissionKey="hermes_agent.seo"
      fields={[
        { name: 'brand', label: '品牌名称', required: true, placeholder: '例如：GlowLab' },
        { name: 'product', label: '核心产品', required: true, placeholder: '例如：玻尿酸补水面膜' },
        { name: 'keywords', label: '目标关键词', placeholder: '多个关键词可用换行分隔' },
        { name: 'target_audience', label: '目标受众', placeholder: '例如：18-30 岁女性，护肤新手' },
      ]}
    />
  )
}
