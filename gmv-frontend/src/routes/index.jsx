// src/routes/index.jsx
import { Suspense, lazy } from 'react';
import { createBrowserRouter, Navigate } from 'react-router-dom';

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
import AutomationControlCenterPage from '../features/platform/gmvmax/pages/AutomationControlCenterPage.jsx';
// 平台管理员 · 列表页
import AdminList from '../features/platform/admin/pages/AdminList.jsx';
// 平台管理员 · OAuth Provider Apps
import OAuthAppsPage from '../features/platform/oauth/pages/OAuthAppsPage.jsx';
// 平台管理员 · 邮件服务器
import EmailServerSettings from '../features/platform/email/pages/EmailServerSettings.jsx';

// 平台 - API Key 管理
import PlatformKieKeyPage from '../features/platform/kie_ai/pages/PlatformKieKeyPage.jsx';
// 平台 - yt-dlp Cookies 管理
import YtDlpCookiesPage from '../features/platform/yt_dlp_cookies/pages/YtDlpCookiesPage.jsx';
import PlatformWebShellPage from '../features/platform/webshell/pages/PlatformWebShellPage.jsx';

// 公司域：成员
import UserList from '../features/tenants/users/pages/UserList.jsx';
import UserCreate from '../features/tenants/users/pages/UserCreate.jsx';
import UserEdit from '../features/tenants/users/pages/UserEdit.jsx';

// 公司域：TikTok Business 授权 + GMV Max
import TbAuthList from '../features/tenants/integrations/tiktok_business/pages/TbAuthList.jsx';
import TbAuthDetail from '../features/tenants/integrations/tiktok_business/pages/TbAuthDetail.jsx';
import TiktokShopAuthList from '../features/tenants/integrations/tiktok_shop/pages/TiktokShopAuthList.jsx';
import Loading from '../components/ui/Loading.jsx';
import GmvMaxErrorBoundary from '../features/tenants/gmv_max/components/GmvMaxErrorBoundary.jsx';

const GmvMaxOverviewPage = lazy(() =>
  import('../features/tenants/gmv_max/pages/GmvMaxOverviewPage.jsx'),
);
const GmvMaxCampaignDetailPage = lazy(() =>
  import('../features/tenants/gmv_max/pages/GmvMaxCampaignDetailPage.jsx'),
);
const WebsiteAdsPage = lazy(() =>
  import('../features/tenants/website_ads/WebsiteAdsPage.jsx'),
);
const CommerceOverviewPage = lazy(() =>
  import('../features/tenants/commerce/CommerceOverviewPage.jsx'),
);
const ProductSettingsPage = lazy(() =>
  import('../features/tenants/commerce/ProductSettingsPage.jsx'),
);
const TiktokShopVideoAnalyticsPage = lazy(() =>
  import('../features/tenants/integrations/tiktok_shop/pages/TiktokShopVideoAnalyticsPage.jsx'),
);

// 租户 - AI 视频页面 + Whisper 工具
import GenerateVideoPage from '../features/tenants/kie_ai/pages/GenerateVideoPage.jsx';
import AiVideoMemberTasksPage from '../features/tenants/kie_ai/pages/AiVideoMemberTasksPage.jsx';
import SubtitleRecognitionPage from '../features/tenants/openai_whisper/pages/SubtitleRecognitionPage.jsx';
import SeoPage from '../features/tenants/hermes_agent/pages/SeoPage.jsx';
import GeoPage from '../features/tenants/hermes_agent/pages/GeoPage.jsx';
import VideoAnalysisPage from '../features/tenants/hermes_agent/pages/VideoAnalysisPage.jsx';
import ScriptPage from '../features/tenants/hermes_agent/pages/ScriptPage.jsx';
import ContentFactoryPage from '../features/tenants/hermes_agent/pages/ContentFactoryPage.jsx';
import ContentFactoryMemberProjectsPage from '../features/tenants/hermes_agent/pages/ContentFactoryMemberProjectsPage.jsx';

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

          {
            path: 'tenants/:wid/overview',
            element: (
              <TenantGuard>
                <Suspense fallback={<Loading text="经营数据加载中…" />}>
                  <CommerceOverviewPage />
                </Suspense>
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/products',
            element: (
              <TenantGuard>
                <Suspense fallback={<Loading text="商品设置加载中…" />}>
                  <ProductSettingsPage />
                </Suspense>
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/settings',
            element: <Navigate to="../products" replace />,
          },

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
              { path: 'gmvmax/monitoring-strategies', element: <AutomationControlCenterPage /> },
              { path: 'apis', element: <ApiDocsView /> },
              { path: 'oauth-apps', element: <OAuthAppsPage /> },
              { path: 'email', element: <EmailServerSettings /> },
              // 平台 - API Key 管理
              { path: 'api-keys', element: <PlatformKieKeyPage /> },
              // ★ 新增：平台 - yt-dlp Cookies 管理
              { path: 'yt-dlp-cookies', element: <YtDlpCookiesPage /> },
              { path: 'webshell', element: <PlatformWebShellPage /> },
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

          {
            path: 'tenants/:wid/tiktok-shop',
            element: (
              <TenantGuard>
                <TiktokShopAuthList />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/tiktok-shop/videos',
            element: (
              <TenantGuard>
                <Suspense fallback={<Loading text="短视频分析加载中…" />}>
                  <TiktokShopVideoAnalyticsPage />
                </Suspense>
              </TenantGuard>
            ),
          },

          // 公司域 - GMV Max
          {
            path: 'tenants/:wid/gmvmax',
            element: (
              <TenantGuard>
                <Suspense fallback={<Loading text="GMV Max 加载中…" />}>
                  <GmvMaxOverviewPage />
                </Suspense>
              </TenantGuard>
            ),
            errorElement: <GmvMaxErrorBoundary />,
          },
          {
            path: 'tenants/:wid/gmvmax/:campaignId',
            element: (
              <TenantGuard>
                <Suspense fallback={<Loading text="GMV Max 加载中…" />}>
                  <GmvMaxCampaignDetailPage />
                </Suspense>
              </TenantGuard>
            ),
            errorElement: <GmvMaxErrorBoundary />,
          },
          {
            path: 'tenants/:wid/website-ads',
            element: (
              <TenantGuard>
                <Suspense fallback={<Loading text="独立站广告加载中…" />}>
                  <WebsiteAdsPage />
                </Suspense>
              </TenantGuard>
            ),
          },

          // 公司域 - 按模型生成视频
          {
            path: 'tenants/:wid/ai-video',
            element: (
              <TenantGuard>
                <GenerateVideoPage />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/ai-video/member-tasks',
            element: (
              <TenantGuard>
                <AiVideoMemberTasksPage />
              </TenantGuard>
            ),
          },


          {
            path: 'tenants/:wid/hermes-agent/content-factory',
            element: (
              <TenantGuard>
                <ContentFactoryPage />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/hermes-agent/member-content-factory',
            element: (
              <TenantGuard>
                <ContentFactoryMemberProjectsPage />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/hermes-agent/seo',
            element: (
              <TenantGuard>
                <SeoPage />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/hermes-agent/geo',
            element: (
              <TenantGuard>
                <GeoPage />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/hermes-agent/video-analysis',
            element: (
              <TenantGuard>
                <VideoAnalysisPage />
              </TenantGuard>
            ),
          },
          {
            path: 'tenants/:wid/hermes-agent/script',
            element: (
              <TenantGuard>
                <ScriptPage />
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
