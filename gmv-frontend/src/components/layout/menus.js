// src/components/layout/menus.js

import { parseBoolLike } from '../../utils/booleans.js'

// 判断公司管理员

function hasHermesPermission(session, key) {
  const perms = session?.permissions || session?.perms || [];
  if (!Array.isArray(perms) || perms.length === 0) return true;
  if (perms.includes('hermes_agent.use') || perms.includes(key)) return true;

  // The menu does not have access to backend Hermes capability flags. Avoid hiding
  // Hermes entry points for deployments where members are allowed by default and
  // sessions also contain unrelated non-Hermes permissions; the page and API still
  // perform the authoritative permission checks.
  return !perms.some((perm) => typeof perm === 'string' && perm.startsWith('hermes_agent.'));
}

function isCompanyAdmin(session) {
  const role = (session?.role || '').toLowerCase();
  return role === 'owner' || role === 'admin';
}

/**
 * 根据会话信息构建菜单分组
 * 每个分组形如：{ title: '分组名', items: [{ to, label, exact? }] }
 */
export function buildMenus(session) {
  const wsId = session?.workspace_id || '';

  // 兼容服务端字段命名 isPlatformAdmin / is_platform_admin
  const adminFlag = session?.isPlatformAdmin ?? session?.is_platform_admin;
  const isPlatformAdmin = parseBoolLike(adminFlag);

  if (isPlatformAdmin) {
    // 平台管理员：仅平台级入口
    return [
      {
        title: '平台',
        items: [
          { to: '/platform',             label: '系统控制台', exact: true },
          { to: '/platform/tenants',     label: '公司管理' },
          { to: '/platform/oauth-apps',  label: 'OAuth 应用' },
          { to: '/platform/gmvmax/monitoring-strategies', label: '自动化运行中心' },
          { to: '/platform/policies',    label: 'API 护栏（高级）' },
          { to: '/platform/apis',        label: 'API 管理' },
          { to: '/platform/email',       label: '邮件服务器' },
          { to: '/platform/api-keys',    label: 'API Key 管理' },
          { to: '/platform/flow2api',    label: 'Flow 视频号池' },
          { to: '/platform/jimeng-lab',  label: '即梦实验' },
          { to: '/platform/doubao-lab',  label: '豆包 Seedance 号池' },
          { href: '/api/v1/platform/sub2api/sso', label: 'Sub2API 管理', external: true },
          { to: '/platform/yt-dlp-cookies', label: 'yt-dlp Cookies 管理' },
          { to: '/platform/webshell',    label: 'WebShell 终端' },
        ],
      },
    ];
  }

  if (isCompanyAdmin(session)) {
    // 公司管理员（owner/admin）
    return [
      {
        title: '公司管理',
        items: [
          { to: `/tenants/${wsId}/overview`, label: '数据总览' },
          { to: `/tenants/${wsId}/users`,    label: '成员管理' },
          { to: `/tenants/${wsId}/products`, label: '商品设置' },
        ],
      },
      {
        title: '整合与授权',
        items: [
          { to: `/tenants/${wsId}/tiktok-business`, label: 'TikTok Business 授权' },
          { to: `/tenants/${wsId}/tiktok-shop`, label: 'TikTok Shop 授权' },
          { to: `/tenants/${wsId}/tiktok-shop/videos`, label: '内容运营中心' },
          { to: `/tenants/${wsId}/tiktok-shop/content-posting`, label: '视频发布' },
          { to: `/tenants/${wsId}/gmvmax`, label: 'GMV Max 管理' },
          { to: `/tenants/${wsId}/website-ads`, label: '独立站广告' },
        ],
      },
      {
        title: 'AI 视频',
        items: [
          { to: `/tenants/${wsId}/ai-video`,         label: '生成视频' },
          { to: `/tenants/${wsId}/ai-video/member-tasks`, label: '成员任务记录' },
        ],
      },

      {
        title: 'Hermes 助手',
        items: [
          { to: `/tenants/${wsId}/hermes-agent/content-factory`, label: '内容工厂' },
          { to: `/tenants/${wsId}/hermes-agent/member-content-factory`, label: '成员内容工厂' },
          ...(hasHermesPermission(session, 'hermes_agent.seo') ? [{ to: `/tenants/${wsId}/hermes-agent/seo`, label: '品牌 SEO 助手' }] : []),
          ...(hasHermesPermission(session, 'hermes_agent.geo') ? [{ to: `/tenants/${wsId}/hermes-agent/geo`, label: 'GEO / AI 搜索优化助手' }] : []),
          ...(hasHermesPermission(session, 'hermes_agent.video_analysis') ? [{ to: `/tenants/${wsId}/hermes-agent/video-analysis`, label: '短视频拆解助手' }] : []),
          ...(hasHermesPermission(session, 'hermes_agent.script') ? [{ to: `/tenants/${wsId}/hermes-agent/script`, label: '短视频脚本助手' }] : []),
        ],
      },

      {
        title: '常用工具',
        items: [
          { to: `/tenants/${wsId}/openai-whisper/subtitles`, label: '识别字幕' },
        ],
      },
    ];
  }

  // 普通成员
  return [
    {
      title: '工作台',
      items: [
        { to: `/tenants/${wsId}/overview`, label: '数据总览' },
        { to: `/tenants/${wsId}/users`, label: '成员' },
        { to: `/tenants/${wsId}/tiktok-shop/videos`, label: '内容运营中心' },
        { to: `/tenants/${wsId}/gmvmax`, label: 'GMV Max 管理' },
        { to: `/tenants/${wsId}/website-ads`, label: '独立站广告' },
        { to: `/tenants/${wsId}/ai-video`, label: '生成视频' },
      ],
    },

    {
      title: 'Hermes 助手',
      items: [
        { to: `/tenants/${wsId}/hermes-agent/content-factory`, label: '内容工厂' },
        ...(hasHermesPermission(session, 'hermes_agent.seo') ? [{ to: `/tenants/${wsId}/hermes-agent/seo`, label: '品牌 SEO 助手' }] : []),
        ...(hasHermesPermission(session, 'hermes_agent.geo') ? [{ to: `/tenants/${wsId}/hermes-agent/geo`, label: 'GEO / AI 搜索优化助手' }] : []),
        ...(hasHermesPermission(session, 'hermes_agent.video_analysis') ? [{ to: `/tenants/${wsId}/hermes-agent/video-analysis`, label: '短视频拆解助手' }] : []),
        ...(hasHermesPermission(session, 'hermes_agent.script') ? [{ to: `/tenants/${wsId}/hermes-agent/script`, label: '短视频脚本助手' }] : []),
      ],
    },

    {
      title: '常用工具',
      items: [
        { to: `/tenants/${wsId}/openai-whisper/subtitles`, label: '识别字幕' },
      ],
    },
  ];
}
