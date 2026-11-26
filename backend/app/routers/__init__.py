"""业务路由汇总：所有模块共用一套资源路由，语义化专项路由单独注册。"""
from app.routers import goods_flow, resource

ROUTERS = [goods_flow, resource]
