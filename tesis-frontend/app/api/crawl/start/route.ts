import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";

const BACKEND_API_URL = process.env.BACKEND_API_URL || "http://localhost:8000";

export async function POST(request: Request) {
  try {
    const { userId } = await auth();
    if (!userId) {
      return NextResponse.json({ detail: "No autenticado." }, { status: 401 });
    }

    const body = await request.json();
    const response = await fetch(`${BACKEND_API_URL}/api/scrape`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, clerk_user_id: userId }),
      cache: "no-store",
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      return NextResponse.json(
        { detail: data?.detail || "No se pudo iniciar el scraping." },
        { status: response.status },
      );
    }

    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { detail: "No se pudo conectar con el backend de scraping." },
      { status: 500 },
    );
  }
}
