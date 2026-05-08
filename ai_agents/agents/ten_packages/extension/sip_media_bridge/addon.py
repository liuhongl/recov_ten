from ten_runtime import Addon, TenEnv, register_addon_as_extension


@register_addon_as_extension("sip_media_bridge")
class SipMediaBridgeAddon(Addon):
    def on_create_instance(self, ten_env: TenEnv, name: str, context) -> None:
        from .extension import SipMediaBridgeExtension

        ten_env.log_info("sip_media_bridge creating extension instance")
        ten_env.on_create_instance_done(
            SipMediaBridgeExtension(name),
            context,
        )


@register_addon_as_extension("sip_media_bridge_test_echo")
class SipMediaBridgeTestEchoAddon(Addon):
    def on_create_instance(self, ten_env: TenEnv, name: str, context) -> None:
        from .test_echo_extension import SipMediaBridgeTestEchoExtension

        ten_env.log_info("sip_media_bridge_test_echo creating extension instance")
        ten_env.on_create_instance_done(
            SipMediaBridgeTestEchoExtension(name),
            context,
        )
