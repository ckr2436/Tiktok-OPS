import HermesAgentPage from './HermesAgentPage.jsx'

export default function ScriptPage() {
  return (
    <HermesAgentPage
      title="短视频脚本助手"
      description="根据品牌、产品与主题自动生成可执行短视频脚本，可用于拍摄与投放。"
      endpoint="script"
      permissionKey="hermes_agent.script"
      fields={[
        { name: 'brand', label: '品牌名称', required: true },
        { name: 'product', label: '产品名称', required: true },
        { name: 'topic', label: '脚本主题', required: true, placeholder: '例如：春季换季保湿' },
        { name: 'tone', label: '语气风格', placeholder: '例如：专业、亲和、反差感' },
        { name: 'duration', label: '目标时长', placeholder: '例如：30 秒' },
      ]}
    />
  )
}
