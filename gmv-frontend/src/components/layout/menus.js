// src/components/layout/menus.js

import { parseBoolLike } from '../../utils/booleans.js'

/**
 * 根据会话信息构建菜单分组
 * 每个分组形如：{ title: '分组名', items: [{ to, label, exact? }] }
 */
export function buildMenus(session) {
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
          { to: '/platform/apis',        label: 'API 管理' },
        ],
      },
    ];
  }

  return [
    {
      title: '数据',
      items: [
        { to: '/tenant/data-overview', label: '数据概览', exact: true },
      ],
    },
  ];
}

