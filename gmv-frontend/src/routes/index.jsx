// src/routes/index.jsx
import { createBrowserRouter } from 'react-router-dom';

// 布局
import AppLayout from '../components/layout/AppLayout.jsx';
import MinimalLayout from '../components/layout/MinimalLayout.jsx';
import AdminLayout from '../components/layout/AdminLayout.jsx'; // 目前没直接用到，但保留

// 守卫
import ProtectedRoute from '../core/ProtectedRoute.jsx';
import AdminOnly from '../core/AdminOnly.jsx';

// 通用页面
import Dashboard from '../pages/Dashboard.jsx';
import NotFound from '../pages/NotFound.jsx';

// 平台认证
import LoginView from '../features/platform/auth/pages/LoginView.jsx';

// 平台（系统级）与公司管理
import AdminHome from '../features/platform/admin/pages/AdminHome.jsx';
import TenantList from '../features/platform/tenants/pages/TenantList.jsx';
import TenantCreate from '../features/platform/tenants/pages/TenantCreate.jsx';
import BcAdsPlanConfig from '../features/platform/admin/pages/BcAdsPlanConfig.jsx';

// 平台管理员 · API 文档
import ApiDocsView from '../features/platform/admin/pages/ApiDocsView.jsx';
// 平台管理员 · 列表页
import AdminList from '../features/platform/admin/pages/AdminList.jsx';
// 平台管理员 · OAuth Provider Apps
import OAuthAppsPage from '../features/platform/oauth/pages/OAuthAppsPage.jsx';

// 租户 · BC Ads 运营计划
import BcAdsPlanSync from '../features/tenants/bc_ads_shop_product/pages/BcAdsPlanSync.jsx';

// 公司域：成员
import UserList from '../features/tenants/users/pages/UserList.jsx';
import UserCreate from '../features/tenants/users/pages/UserCreate.jsx';
import UserEdit from '../features/tenants/users/pages/UserEdit.jsx';

// 公司域：TikTok Business 授权
import TbAuthList from '../features/tenants/integrations/tiktok_business/pages/TbAuthList.jsx';
import TbAuthDetail from '../features/tenants/integrations/tiktok_business/pages/TbAuthDetail.jsx';

const router = createBrowserRouter([
  // 登录页
  { path: '/login', element: <MinimalLayout><LoginView /></MinimalLayout> },

  // 受保护区域
  {
    path: '/',
    element: <ProtectedRoute />,
    children: [
      {
        element: <AppLayout />,
        children: [
          { index: true, element: <Dashboard /> },
          { path: 'dashboard', element: <Dashboard /> },

          // 平台控制台（仅平台管理员）
          {
            path: 'platform',
            element: <AdminOnly />,
            children: [
              { index: true, element: <AdminHome /> },
              { path: 'admins', element: <AdminList /> },
              { path: 'tenants', element: <TenantList /> },
              { path: 'tenants/create', element: <TenantCreate /> },
              { path: 'apis', element: <ApiDocsView /> },
              { path: 'oauth-apps', element: <OAuthAppsPage /> },
              { path: 'bc-ads-shop-product', element: <BcAdsPlanConfig /> },
            ],
          },

          // 公司域 - 成员
          { path: 'tenants/:wid/users', element: <UserList /> },
          { path: 'tenants/:wid/users/create', element: <UserCreate /> },
          { path: 'tenants/:wid/users/:uid', element: <UserEdit /> },

          // 公司域 - TikTok Business 授权
          { path: 'tenants/:wid/tiktok_business', element: <TbAuthList /> },
          { path: 'tenants/:wid/tiktok_business/:auth_id', element: <TbAuthDetail /> },
          { path: 'tenants/:wid/bc_ads_shop_product', element: <BcAdsPlanSync /> },

        ],
      },
    ],
  },

  // 兜底
  { path: '*', element: <NotFound /> },
]);

export default router;

