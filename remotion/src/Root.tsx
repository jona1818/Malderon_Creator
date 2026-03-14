import React from "react";
import { registerRoot, Composition, CalculateMetadataFunction } from "remotion";
import { TitleCard } from "./TitleCard";
import type { TitleCardProps } from "./TitleCard";

const calculateMetadata: CalculateMetadataFunction<TitleCardProps> = ({
  props,
}) => {
  return {
    durationInFrames: props.durationInFrames || 150,
    props,
  };
};

const RemotionRoot: React.FC = () => {
  return (
    <Composition<TitleCardProps>
      id="TitleCard"
      component={TitleCard}
      width={1920}
      height={1080}
      fps={30}
      durationInFrames={150}
      defaultProps={{
        titleText: "#10 Title Example",
        durationInFrames: 150,
        backgroundImage: null,
      }}
      calculateMetadata={calculateMetadata}
    />
  );
};

registerRoot(RemotionRoot);
