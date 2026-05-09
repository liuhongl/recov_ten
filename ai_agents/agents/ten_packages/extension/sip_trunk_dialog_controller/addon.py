from ten_runtime import Addon, TenEnv, register_addon_as_extension


@register_addon_as_extension("sip_trunk_dialog_controller")
class SipTrunkDialogControllerAddon(Addon):
    def on_create_instance(
        self,
        ten_env: TenEnv,
        name: str,
        context,
    ) -> None:
        from .extension import SipTrunkDialogControllerExtension

        ten_env.log_info(
            "sip_trunk_dialog_controller creating extension instance"
        )
        ten_env.on_create_instance_done(
            SipTrunkDialogControllerExtension(name), context
        )
