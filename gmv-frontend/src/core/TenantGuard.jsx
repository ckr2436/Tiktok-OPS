import { useSelector } from "react-redux";
import { useParams, Navigate, useLocation } from "react-router-dom";

export default function TenantGuard({ children }) {
  const { wid } = useParams();
  const location = useLocation();
  const checked = useSelector((s) => s.session?.checked);
  const session = useSelector((s) => s.session?.data || {});
  const myWid = String(session?.workspace_id ?? "").trim();
  const isPlatformAdmin = !!(session?.is_platform_admin ?? session?.isPlatformAdmin);

  if (!checked) {
    return null;
  }

  if (isPlatformAdmin) {
    return (
      <Navigate
        to="/dashboard"
        replace
        state={{ err: "403: 平台管理员无权访问租户业务数据" }}
      />
    );
  }

  if (myWid && String(wid) !== myWid) {
    const msg = `403: 该页面属于租户 ${wid}，你不在此租户名下`;
    if (typeof window !== "undefined" && window?.console) {
      console.warn(msg, location.pathname);
    }
    return (
      <Navigate
        to={`/tenants/${encodeURIComponent(myWid)}/tiktok-business`}
        replace
        state={{ err: msg }}
      />
    );
  }

  return children;
}
