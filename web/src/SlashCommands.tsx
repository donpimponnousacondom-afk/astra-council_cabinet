import type { RecordData } from "./api";
import { Badge, Notice } from "./components";

export function SlashCommandSetup({
  bot,
  draft,
}: {
  bot?: RecordData;
  draft: RecordData;
}) {
  if (!draft.enabled_plugins?.includes("slash_commands")) return null;
  if (draft.role !== "council")
    return (
      <Notice>
        Slash commands are available to council companions such as Loki.
        Hortator retains its existing control-channel and owner-DM behavior.
      </Notice>
    );
  const state = bot?.slash_commands;
  return (
    <fieldset>
      <legend>Slash command setup</legend>
      <p>
        Enable <strong>Slash command assistant</strong> globally in Plugins,
        grant it here and save. The bot must be enabled and connected. Existing
        room chat continues normally.
      </p>
      <p>
        In this application's Discord Developer Portal → Installation, enable{" "}
        <strong>User Install</strong> and <strong>Guild Install</strong>. Choose
        Discord Provided Link. User Install uses{" "}
        <code>applications.commands</code>; Guild Install uses <code>bot</code>{" "}
        and <code>applications.commands</code>. Retain the bot's normal room
        permissions.
      </p>
      {state && (
        <p>
          <Badge tone={state.registered ? "green" : "amber"}>
            {state.enabled
              ? state.registered
                ? "/prompt registered"
                : "Awaiting /prompt registration"
              : "Slash plugin inactive"}
          </Badge>{" "}
          Registration failures appear under Discord events.
        </p>
      )}
      {state?.user_install_url && (
        <p>
          <a href={state.user_install_url} target="_blank" rel="noreferrer">
            Install this application's commands to your Discord account
          </a>
        </p>
      )}
      <p>
        <code>/prompt text:&lt;your request&gt; private:true</code> starts a
        fresh conversation. Only the configured owner may invoke it. Granted
        notes and tools are available; surrounding channel history is not read.
        Set <code>private:false</code> to post the result in that channel. Slash
        requests stop after 14 minutes; saved work remains and is not
        automatically retried.
      </p>
    </fieldset>
  );
}
