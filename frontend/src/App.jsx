import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, RouterProvider, createBrowserRouter } from "react-router-dom";
import { ToastProvider } from "./components/common/Toast";
import { AppShell } from "./layout/AppShell";
import { UploadPage } from "./pages/upload/UploadPage";
import { UploadProgressPage } from "./pages/upload/UploadProgressPage";
import { ProcessingQueuePage } from "./pages/processing/ProcessingQueuePage";
import { ReviewQueuePage } from "./pages/review/ReviewQueuePage";
import { ReviewDetailPage } from "./pages/review/ReviewDetailPage";
import { SchemasListPage } from "./pages/schemas/SchemasListPage";
import { SchemaCreatePage } from "./pages/schemas/SchemaCreatePage";
import { SchemaRecordsPage } from "./pages/schemas/SchemaRecordsPage";
import { SchemaEditPage } from "./pages/schemas/SchemaEditPage";
import { TopicsListPage } from "./pages/topics/TopicsListPage";
import { QueryPage } from "./pages/query/QueryPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { path: "/", element: <Navigate to="/upload" replace /> },
      { path: "/upload", element: <UploadPage /> },
      { path: "/upload/:batchId/progress", element: <UploadProgressPage /> },
      { path: "/queue", element: <ProcessingQueuePage /> },
      { path: "/review", element: <ReviewQueuePage /> },
      { path: "/review/:jobId", element: <ReviewDetailPage /> },
      { path: "/schemas", element: <SchemasListPage /> },
      { path: "/schemas/new", element: <SchemaCreatePage /> },
      { path: "/schemas/:schemaId", element: <SchemaRecordsPage /> },
      { path: "/schemas/:schemaId/edit", element: <SchemaEditPage /> },
      { path: "/topics", element: <TopicsListPage /> },
      { path: "/query", element: <QueryPage /> },
    ],
  },
]);

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  );
}
