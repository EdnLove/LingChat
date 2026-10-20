package com.lingchat.maidbridge.maid;

import com.google.gson.JsonObject;
import com.lingchat.maidbridge.bridge.Envelope;
import com.lingchat.maidbridge.bridge.MessageHandler;
import com.lingchat.maidbridge.bridge.Protocol;

/**
 * {@code maid_state}：她现在是不是死的。
 *
 * <p>死亡是持久状态，而且死后实体就不在世上，感知与聊天都推不出「她已经倒下」。
 * 插件每轮靠这一条把死亡状态重新写进台词表，模型才不会再把她当成站在旁边的人。
 *
 * <p>{@code known} 表示这一局里模组真的见过她的死活：刚重启时它可能不知道上次留下的
 * 状态，插件据此退回台词表里最后一条死亡/复活旁白。
 */
public class MaidStateHandler implements MessageHandler {
    @Override
    public JsonObject handle(JsonObject request) {
        JsonObject data = new JsonObject();
        data.addProperty("maid_id", MonitoredMaidHandler.get());
        data.addProperty("dead", MonitoredMaidHandler.isDead());
        data.addProperty("known", MonitoredMaidHandler.isStateKnown());
        return Envelope.response(Protocol.MAID_STATE_RESULT, data);
    }
}
