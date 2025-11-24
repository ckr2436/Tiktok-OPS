// src/components/layout/menus.js

import { parseBoolLike } from '../../utils/booleans.js'

// 判断公司管理员
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
          { to: '/platform/policies',    label: 'Platform Policies' },
          { to: '/platform/apis',        label: 'API 管理' },
	  { to: '/platform/kie-ai',      label: 'KIE AI 接入' },
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
          { to: `/tenants/${wsId}/overview`, label: '公司概览' },
          { to: `/tenants/${wsId}/users`,    label: '成员管理' },
          { to: `/tenants/${wsId}/settings`, label: '公司设置' },
        ],
      },
      {
        title: '整合与授权',
        items: [
          { to: `/tenants/${wsId}/tiktok-business`, label: 'TikTok Business 授权' },
          { to: `/tenants/${wsId}/gmvmax`, label: 'GMV Max 管理' },
        ],
      },
      {
        title: 'AI视频',
        items: [
          { to: `/tenants/${wsId}/kie-ai/sora2`,    label: 'Sora2 视频' },
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
        { to: '/dashboard',             label: '仪表盘', exact: true },
        { to: `/tenants/${wsId}/users`, label: '成员' },
        { to: `/tenants/${wsId}/gmvmax`, label: 'GMV Max 管理' },
        { to: `/tenants/${wsId}/kie-ai/sora2`, label: 'KIE Sora2 视频' },
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

