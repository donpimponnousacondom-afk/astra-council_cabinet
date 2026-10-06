import type { RecordData } from "./api";

// Display names never replace the immutable IDs used in requests and routing.
export function channelLabel(
  id: string,
  rooms: RecordData[],
  channels: RecordData[],
): string {
  const room = rooms.find((item) => item.channel_id === id);
  const channel = channels.find((item) => item.channel_id === id);
  const hash = (name: string) => `#${name.replace(/^#/, "")}`;
  if (room?.name) return hash(room.name);
  if (channel?.parent_id) {
    const parent =
      rooms.find((item) => item.channel_id === channel.parent_id)?.name ||
      channel.parent_name;
    if (parent)
      return channel.channel_name
        ? `${hash(parent)} / ${channel.channel_name}`
        : `Thread in ${hash(parent)}`;
    return channel.channel_name
      ? `Thread: ${channel.channel_name}`
      : "Unknown thread";
  }
  return channel?.channel_name ? hash(channel.channel_name) : "Unknown channel";
}

export function channelOption(
  id: string,
  rooms: RecordData[],
  channels: RecordData[],
): string {
  return `${channelLabel(id, rooms, channels)} · ${id}`;
}
