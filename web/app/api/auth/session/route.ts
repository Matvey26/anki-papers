import { getSessionUser } from "@/lib/auth";

export async function GET(request: Request) {
  const user = await getSessionUser(request);
  return Response.json({ user }, { status: user ? 200 : 401 });
}
