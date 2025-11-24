// src/routes/index.jsx
import { createBrowserRouter } from 'react-router-dom';

// 布局
import AppLayout from '../components/layout/AppLayout.jsx';
import MinimalLayout from '../components/layout/MinimalLayout.jsx';
import AdminLayout from '../components/layout/AdminLayout.jsx'; // 目前没直接用到，但保留

// 守卫
import ProtectedRoute from '../core/ProtectedRoute.jsx';
import AdminOnly from '../core/AdminOnly.jsx';
import TenantGuard from '../core/TenantGuard.jsx';

// 通用页面
import Dashboard from '../pages/Dashboard.jsx';
import NotFound from '../pages/NotFound.jsx';

// 平台认证
import LoginView from '../features/platform/auth/pages/LoginView.jsx';

// 平台（系统级）与公司管理
import AdminHome from '../features/platform/admin/pages/AdminHome.jsx';
import TenantList from '../features/platform/tenants/pages/TenantList.jsx';
import TenantCreate from '../features/platform/tenants/pages/TenantCreate.jsx';

// 平台管理员 · API 文档
import ApiDocsView from '../features/platform/admin/pages/ApiDocsView.jsx';
import PlatformPolicies from '../features/platform/admin/pages/PlatformPolicies.jsx';
// 平台管理员 · 列表页
import AdminList from '../features/platform/admin/pages/AdminList.jsx';
// 平台管理员 · OAuth Provider Apps
import OAuthAppsPage from '../features/platform/oauth/pages/OAuthAppsPage.jsx';

// 平台 - KIE AI Key 管理
import PlatformKieKeyPage from '../features/platform/kie_ai/pages/PlatformKieKeyPage.jsx';

// 公司域：成员
import UserList from '../features/tenants/users/pages/UserList.jsx';
import UserCreate from '../features/tenants/users/pages/UserCreate.jsx';
import UserEdit from '../features/tenants/users/pages/UserEdit.jsx';

// 公司域：TikTok Business 授权 + GMV Max
import TbAuthList from '../features/tenants/integrations/tiktok_business/pages/TbAuthList.jsx';
import TbAuthDetail from '../features/tenants/integrations/tiktok_business/pages/TbAuthDetail.jsx';
import GmvMaxOverviewPage from '../features/tenants/gmv_max/pages/GmvMaxOverviewPage.jsx';
import GmvMaxCampaignDetailPage from '../features/tenants/gmv_max/pages/GmvMaxCampaignDetailPage.jsx';

// 租户 - KIE Sora2 页面 + Whisper 工具
import Sora2ImageToVideoPage from '../features/tenants/kie_ai/pages/Sora2ImageToVideoPage.jsx';
import SubtitleRecognitionPage from '../features/tenants/openai_whisper/pages/SubtitleRecognitionPage.jsx';

const router = createBrowserRouter([
  // 登录页
  {
    path: '/login',
    element: (
      <MinimalLayout>
        <LoginView />
      </MinimalLayout>
    ),
  },

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
              { path: 'policies', element: <PlatformPolicies /> },
              { path: 'apis', element: <ApiDocsView /> },
              { path: 'oauth-apps', element: <OAuthAppsPage /> },
              // ★ 新增：平台 - KIE AI Key 管理
              { path: 'kie-ai', element: <PlatformKieKeyPage /> },
            ],
          },

          // 公司域 - 成员
          {
            path: 'tenants/:wid/users',
            element: (
              <TenantGuard>
                <UserList />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/users/create',
            element: (
              <TenantGuard>
                <UserCreate />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/users/:uid',
            element: (
              <TenantGuard>
                <UserEdit />
              </TenantGuard>
            ),
          },

          // 公司域 - TikTok Business 授权
          {
            path: 'tenants/:wid/tiktok-business',
            element: (
              <TenantGuard>
                <TbAuthList />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/tiktok-business/:auth_id',
            element: (
              <TenantGuard>
                <TbAuthDetail />
              </TenantGuard>
            ),
          },

          // 公司域 - GMV Max
          {
            path: 'tenants/:wid/gmvmax',
            element: (
              <TenantGuard>
                <GmvMaxOverviewPage />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/gmvmax/:campaignId',
            element: (
              <TenantGuard>
                <GmvMaxCampaignDetailPage />
              </TenantGuard>
            ),
          },

          // ★ 新增：公司域 - KIE Sora2 视频
          {
            path: 'tenants/:wid/kie-ai/sora2',
            element: (
              <TenantGuard>
                <Sora2ImageToVideoPage />
              </TenantGuard>
            ),
          },

          {
            path: 'tenants/:wid/openai-whisper/subtitles',
            element: (
              <TenantGuard>
                <SubtitleRecognitionPage />
              </TenantGuard>
            ),
          },

        ],
      },
    ],
  },

  // 兜底
  { path: '*', element: <NotFound /> },
]);

export default router;

