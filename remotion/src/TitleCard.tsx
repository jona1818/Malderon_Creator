import React from "react";
import {
  AbsoluteFill,
  Img,
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
  staticFile,
  Easing,
} from "remotion";
import { loadFont } from "@remotion/google-fonts/Oswald";

const { fontFamily } = loadFont("normal", {
  weights: ["500", "700"],
  subsets: ["latin", "latin-ext"],
});

export type TitleCardProps = {
  titleText: string;
  durationInFrames: number;
  backgroundImage: string | null; // filename in public/ or null for gradient
};

export const TitleCard: React.FC<TitleCardProps> = ({
  titleText,
  backgroundImage,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  // ── Box slide-in from left (snappy) ──
  const boxEntrance = spring({
    frame,
    fps,
    config: { damping: 20, stiffness: 200 },
  });

  // ── Text fade-in (slightly delayed) ──
  const textOpacity = interpolate(frame, [6, 14], [0, 1], {
    extrapolateRight: "clamp",
    extrapolateLeft: "clamp",
  });

  // ── Accent line width animation ──
  const lineWidth = interpolate(frame, [8, 20], [0, 1], {
    extrapolateRight: "clamp",
    extrapolateLeft: "clamp",
    easing: Easing.out(Easing.quad),
  });

  // ── Exit animation (last 0.4s) ──
  const exitStart = durationInFrames - 0.4 * fps;
  const exitProgress = interpolate(
    frame,
    [exitStart, durationInFrames],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const exitTranslateX = interpolate(exitProgress, [0, 1], [0, -80]);
  const exitOpacity = interpolate(exitProgress, [0, 1], [1, 0]);

  // ── Ken Burns zoom for background image ──
  const bgScale = interpolate(frame, [0, durationInFrames], [1.0, 1.08], {
    extrapolateRight: "clamp",
    extrapolateLeft: "clamp",
  });

  // Split title if it contains a number prefix like "#10"
  const parts = titleText.match(/^(#\d+)\s*(.*)$/);
  const numberPart = parts ? parts[1] : null;
  // Safety: truncate to max ~40 chars to prevent overflow
  const rawText = parts ? parts[2] : titleText;
  const textPart = rawText.length > 40 ? rawText.slice(0, 37) + "..." : rawText;

  const hasBackground = Boolean(backgroundImage);

  // Auto-scale font size based on text length to prevent overflow
  const textLen = textPart.length;
  const baseFontSize = numberPart ? 52 : 64;
  const fontSize = textLen > 30 ? Math.max(32, baseFontSize - (textLen - 30) * 1.2)
    : textLen > 20 ? Math.max(36, baseFontSize - (textLen - 20) * 0.8)
    : baseFontSize;

  // Box slide from left
  const boxTranslateX = interpolate(boxEntrance, [0, 1], [-600, 0]);

  return (
    <AbsoluteFill
      style={{
        background: hasBackground
          ? "#000"
          : "linear-gradient(135deg, #0a0a0a 0%, #1a1a2e 100%)",
        justifyContent: "flex-end",
        alignItems: "flex-start",
      }}
    >
      {/* Background image with Ken Burns zoom */}
      {hasBackground && (
        <AbsoluteFill style={{ transform: `scale(${bgScale})` }}>
          <Img
            src={staticFile(backgroundImage!)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
            }}
          />
        </AbsoluteFill>
      )}

      {/* Gradient overlay for text readability */}
      {hasBackground && (
        <AbsoluteFill
          style={{
            background:
              "linear-gradient(0deg, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.4) 40%, rgba(0,0,0,0.1) 100%)",
          }}
        />
      )}

      {/* Title box — bottom-left, dark background pill */}
      <div
        style={{
          position: "absolute",
          bottom: 120,
          left: 0,
          maxWidth: 1820,
          transform: `translateX(${boxTranslateX + exitTranslateX}px)`,
          opacity: exitOpacity,
          display: "flex",
          flexDirection: "column",
          gap: 0,
        }}
      >
        {/* Dark box with title text */}
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 16,
            background: "rgba(0, 0, 0, 0.85)",
            padding: numberPart ? "20px 50px 20px 50px" : "24px 50px",
          }}
        >
          {/* Number badge */}
          {numberPart && (
            <div
              style={{
                fontFamily,
                fontWeight: 700,
                fontSize: Math.min(72, fontSize * 1.3),
                color: "#ffffff",
                letterSpacing: 2,
                opacity: textOpacity,
                marginRight: 8,
                whiteSpace: "nowrap",
              }}
            >
              {numberPart}
            </div>
          )}

          {/* Title text */}
          <div
            style={{
              fontFamily,
              fontWeight: 500,
              fontSize,
              color: "#ffffff",
              textTransform: "uppercase",
              letterSpacing: 3,
              opacity: textOpacity,
              lineHeight: 1.15,
              wordBreak: "break-word",
            }}
          >
            {textPart}
          </div>
        </div>

        {/* Accent line below the box */}
        <div
          style={{
            height: 4,
            width: `${lineWidth * 100}%`,
            background: "linear-gradient(90deg, #e50914, #ff6b35)",
            marginLeft: 0,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};
